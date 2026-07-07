"""
test_parser_district_alias_bugs.py — Regression tests for three live-reported
parser bugs (Travel Guild staging, 2026-07-01).

Bug 1 — "beaches" plural not recognised as a vibe:
  User: "7 days Bali beaches, $1200, solo"
  Bug: "beaches" fell through the vibe synonym map → no det_vibe_seq →
       multi-area clarification gate fired even though the intent was fully
       specified.
  Fix: added "beaches" → "beach" to _VIBE_SYNONYMS.

Bug 2 — Bali sub-districts rejected with cannot_satisfy:
  User follow-up (after area clarification): "I'd like Canggu and Ubud"
  Bug: "Canggu", "Ubud", etc. were not in CITY_ALIASES → honest-decline
       "destination 'Canggu' is not in the supported catalog."
  Fix: added all Bali district names to CITY_ALIASES (mapping to "bali").

Bug 3 — Area clarification message listed district names as bookable destinations:
  Bug: the clarification text read "Bali has 8 distinct areas: canggu,
       jimbaran, kuta …" — this invited users to type district names as
       destinations, causing Bug 2.
  Fix: removed the area list from the clarification message; now only asks
       for a vibe without implying districts are standalone destinations.

All tests are var-0 / CI-safe: no LLM, no network, no paid API.
The DashScope key is suppressed so the deterministic regex/fallback path runs.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import intent_parser as ip  # noqa: E402


def _no_llm():
    """Suppress DashScope so tests run purely on the deterministic fallback."""
    return patch("utils.intent_parser.DASHSCOPE_API_KEY", "")


# ===========================================================================
# Bug 1 — "beaches" plural must map to vibe "beach"
# ===========================================================================

class TestBeachesPluralVibe(unittest.TestCase):
    """'beaches' (user-typed plural) must resolve to the canonical 'beach' vibe."""

    def test_vibe_synonym_beaches_maps_to_beach(self):
        """Unit: _normalise_vibe('beaches') == 'beach'."""
        self.assertEqual(ip._normalise_vibe("beaches"), "beach")

    def test_vibe_synonym_beaches_plural_in_synonym_map(self):
        """'beaches' must be a key in the _VIBE_SYNONYMS dict."""
        self.assertIn("beaches", ip._VIBE_SYNONYMS)
        self.assertEqual(ip._VIBE_SYNONYMS["beaches"], "beach")

    def test_full_request_beaches_plural_no_clarification(self):
        """'7 days Bali beaches, $1200, solo' — fully specified → no clarification.

        Before the fix the 'beaches' vibe was not detected, so det_vibe_seq was
        empty and the multi-area clarification gate fired unnecessarily.
        """
        with _no_llm():
            result = ip.parse_intent("7 days Bali beaches, $1200, solo")
        # Must NOT trigger clarification: destination+budget+duration+vibe all present.
        self.assertFalse(
            result.get("needs_clarification"),
            f"Expected no clarification but got: {result.get('reason')}",
        )
        # And vibe must be 'beach'.
        legs = result.get("legs", [])
        self.assertTrue(legs, "Expected at least one leg")
        self.assertEqual(legs[0].get("vibe"), "beach")
        self.assertEqual(legs[0].get("city"), "bali")

    def test_full_request_beach_singular_no_clarification(self):
        """Baseline: 'beach' singular was already working — must continue to work."""
        with _no_llm():
            result = ip.parse_intent("7 days Bali beach, $1200, solo")
        self.assertFalse(result.get("needs_clarification"))
        legs = result.get("legs", [])
        self.assertEqual(legs[0].get("vibe"), "beach")


# ===========================================================================
# Bug 2 — Bali district names must alias to "bali"
# ===========================================================================

class TestBaliDistrictAliases(unittest.TestCase):
    """Each Bali district that appears in AREA_CLOSED_SETS must alias to 'bali'
    in CITY_ALIASES, so follow-up text like 'I'd prefer Canggu' parses correctly
    instead of triggering an honest cannot_satisfy decline.
    """

    DISTRICTS = [
        "canggu", "ubud", "seminyak", "kuta",
        "jimbaran", "nusa dua", "legian", "sanur",
        "kerobokan", "uluwatu",
    ]

    def test_all_bali_districts_in_city_aliases(self):
        """CITY_ALIASES must contain every Bali district → 'bali'."""
        for district in self.DISTRICTS:
            with self.subTest(district=district):
                self.assertIn(
                    district, ip.CITY_ALIASES,
                    f"'{district}' missing from CITY_ALIASES",
                )
                self.assertEqual(
                    ip.CITY_ALIASES[district], "bali",
                    f"CITY_ALIASES['{district}'] must equal 'bali'",
                )

    def test_canggu_resolves_to_bali(self):
        """'7 days Canggu beach, $1200' → city='bali', no decline."""
        with _no_llm():
            result = ip.parse_intent("7 days Canggu beach, $1200")
        self.assertFalse(
            result.get("needs_clarification"),
            f"'Canggu' should resolve to 'bali', got: {result.get('reason')}",
        )
        legs = result.get("legs", [])
        self.assertTrue(legs)
        self.assertEqual(legs[0]["city"], "bali")

    def test_ubud_resolves_to_bali(self):
        """'7 days Ubud culture, $1000' → city='bali'."""
        with _no_llm():
            result = ip.parse_intent("7 days Ubud culture, $1000")
        self.assertFalse(
            result.get("needs_clarification"),
            f"'Ubud' should resolve to 'bali', got: {result.get('reason')}",
        )
        legs = result.get("legs", [])
        self.assertTrue(legs)
        self.assertEqual(legs[0]["city"], "bali")

    def test_seminyak_resolves_to_bali(self):
        """'5 days Seminyak relax, $1200' → city='bali'."""
        with _no_llm():
            result = ip.parse_intent("5 days Seminyak relax, $1200")
        self.assertFalse(result.get("needs_clarification"))
        legs = result.get("legs", [])
        self.assertTrue(legs)
        self.assertEqual(legs[0]["city"], "bali")

    def test_kuta_resolves_to_bali(self):
        """'7 days Kuta beach, $1000' → city='bali'."""
        with _no_llm():
            result = ip.parse_intent("7 days Kuta beach, $1000")
        self.assertFalse(result.get("needs_clarification"))
        legs = result.get("legs", [])
        self.assertTrue(legs)
        self.assertEqual(legs[0]["city"], "bali")

    def test_district_followup_not_declined(self):
        """Regression: the exact follow-up that triggered the bug.

        User original: '7 days Bali beaches, $1200, solo'
        Clarification returned (bug was: gate shouldn't have fired, but
        if it does, the follow-up must not be declined).
        User follow-up: 'I don't mind staying in Canggu and Ubud, culture'
        Before fix: cannot_satisfy 'Canggu'.
        After fix: 'Canggu' → 'bali', 'Ubud' → 'bali' → single Bali leg.
        """
        with _no_llm():
            result = ip.parse_intent(
                "I don't mind staying in Canggu and Ubud, "
                "I like culture, budget $1200, 7 nights"
            )
        # Must not be a flat decline for an unknown destination.
        self.assertFalse(
            result.get("outcome") == "cannot_satisfy"
            and "Canggu" in result.get("reason", ""),
            f"'Canggu' triggered a hard decline: {result.get('reason')}",
        )
        # When city is resolved, it must be 'bali'.
        legs = result.get("legs", [])
        for leg in legs:
            self.assertEqual(
                leg.get("city"), "bali",
                f"Expected city='bali', got {leg.get('city')}",
            )

    def test_phuket_district_patong_aliases_to_phuket(self):
        """Same pattern for Phuket: 'patong' → 'phuket'."""
        self.assertIn("patong", ip.CITY_ALIASES)
        self.assertEqual(ip.CITY_ALIASES["patong"], "phuket")


# ===========================================================================
# Bug 3 — Multi-area clarification must NOT list district names as destinations
# ===========================================================================

class TestAreaClarificationMessage(unittest.TestCase):
    """The multi-area clarification message must not present district names as
    standalone bookable destinations — doing so caused users to type 'Canggu'
    as their response, which triggered Bug 2.

    The fix: removed the district list from the clarification; the message now
    only asks for a vibe.
    """

    def _get_bali_clarification(self):
        """Parse a fully-specified Bali request that still lacks a vibe (to ensure
        the gate fires) and return the result."""
        with _no_llm():
            return ip.parse_intent("8 nights Bali, $3000")

    def test_clarification_does_not_list_bali_districts(self):
        """The clarification reason must NOT contain 'distinct areas: canggu' or
        similar text that would invite the user to type a district name.
        """
        result = self._get_bali_clarification()
        if not result.get("needs_clarification"):
            self.skipTest("Multi-area gate did not fire — cannot test message content")
        reason = result.get("reason", "")
        # Old bug: "Bali has 8 distinct areas: canggu, jimbaran, kuta, ..."
        self.assertNotIn(
            "distinct areas",
            reason.lower(),
            f"Clarification message still lists 'distinct areas': {reason!r}",
        )
        self.assertNotIn(
            "canggu",
            reason.lower(),
            f"Clarification message contains 'canggu' (a district name): {reason!r}",
        )
        self.assertNotIn(
            "jimbaran",
            reason.lower(),
            f"Clarification message contains 'jimbaran' (a district name): {reason!r}",
        )

    def test_clarification_asks_for_vibe_not_area_choice(self):
        """The clarification message must mention 'vibe' — that's what the gate is
        now asking for, without listing district names.
        """
        result = self._get_bali_clarification()
        if not result.get("needs_clarification"):
            self.skipTest("Multi-area gate did not fire — cannot test message content")
        reason = result.get("reason", "").lower()
        # The rephrased message asks for a vibe, not an area.
        self.assertIn(
            "vibe",
            reason,
            f"Clarification message should ask for a vibe but doesn't: {reason!r}",
        )

    def test_beaches_plural_suppresses_multi_area_gate(self):
        """End-to-end regression for the exact reported scenario.

        User types: '7 days Bali beaches, $1200, solo'
        The multi-area gate must NOT fire because 'beaches' is now a valid vibe.
        Before Bug 1 fix: gate fired and asked for area/vibe.
        After fix: no clarification — the request is fully specified.
        """
        with _no_llm():
            result = ip.parse_intent("7 days Bali beaches, $1200, solo")
        self.assertFalse(
            result.get("needs_clarification"),
            "Multi-area gate must not fire when 'beaches' (vibe) is present. "
            f"Got: {result.get('reason')}",
        )


if __name__ == "__main__":
    unittest.main()
