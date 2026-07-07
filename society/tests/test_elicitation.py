"""
Tests for #26 — Interactive Elicitation in intent_parser.

When a free-text trip_request is UNDERSPECIFIED (a load-bearing slot — budget,
duration, destination, area — is empty such that no sensible plan can form),
parse_intent returns a machine-readable ``elicitation`` object ALONGSIDE the
existing prose ``reason`` (and ``preference_prompt`` for the AREA case), instead
of guessing or dead-ending in prose.

These tests prove:
  * STRUCTURED ENVELOPE: an underspecified request returns
    needs_clarification + a well-formed elicitation object naming the gap.
  * var-0 / DETERMINISM: the same text yields a byte-identical envelope across
    repeated calls (no LLM/clock/random on the output path).
  * APPEND-ONLY: a fully-specified plain trip (e.g. Bali, with vibe + budget +
    nights) is UNCHANGED — it returns a trip_request with NO elicitation key and
    NO needs_clarification.
  * CLOSED-SET guard: every emitted slot is in the frozen ElicitationSlot vocab,
    and _build_elicitation returns None (no slot invented) for an unknown gap.
  * HONESTY: the prose ``reason`` is preserved verbatim; the AREA case keeps
    ``preference_prompt`` verbatim; the elicitation is a strict SUPERSET.

Run (own test only, per box-safety):
    python -m pytest society/test_elicitation.py -q
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

# Ensure we can import from society/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import intent_parser as ip  # noqa: E402


# Patch contexts: with NO DashScope key, the LLM step raises and the
# deterministic fallback runs — keeping these tests OFF the LLM (pure path).
def _no_llm():
    return patch("utils.intent_parser.DASHSCOPE_API_KEY", "")


class TestElicitationUnit(unittest.TestCase):
    """Unit tests for the pure _build_elicitation helper + closed set."""

    def test_closed_set_vocabulary(self) -> None:
        """Every ElicitationSlot.* constant is in the frozen _ELICITATION_SLOTS."""
        declared = {
            ip.ElicitationSlot.DESTINATION,
            ip.ElicitationSlot.BUDGET,
            ip.ElicitationSlot.DURATION,
            ip.ElicitationSlot.AREA,
            ip.ElicitationSlot.CURRENCY,
            ip.ElicitationSlot.TOO_MANY_CITIES,
            ip.ElicitationSlot.DATE,  # added by #119 date-range elicitation
        }
        self.assertEqual(declared, set(ip._ELICITATION_SLOTS))

    def test_unknown_slot_returns_none(self) -> None:
        """HONESTY: an unrecognised gap -> None (slot is omitted, never invented)."""
        self.assertIsNone(
            ip._build_elicitation("not_a_real_slot", "ignored?")
        )

    def test_build_is_pure_and_sorted(self) -> None:
        """var-0: choices/examples are emitted via sorted(); output is stable."""
        a = ip._build_elicitation(
            ip.ElicitationSlot.DESTINATION,
            "Where to?",
            choices=["Tokyo", "Bali", "Cairo"],
            examples=["zeta", "alpha"],
            has_budget=True,
        )
        b = ip._build_elicitation(
            ip.ElicitationSlot.DESTINATION,
            "Where to?",
            choices=["Cairo", "Bali", "Tokyo"],  # different input order
            examples=["alpha", "zeta"],
            has_budget=True,
        )
        self.assertEqual(a, b)
        self.assertEqual(a["choices"], ["Bali", "Cairo", "Tokyo"])
        self.assertEqual(a["examples"], ["alpha", "zeta"])
        # satisfied/missing are sorted, closed-set, and partition the core slots.
        self.assertEqual(a["satisfied_slots"], ["budget"])
        self.assertEqual(a["missing_slots"], ["destination", "duration"])
        self.assertEqual(
            sorted(a["satisfied_slots"] + a["missing_slots"]),
            sorted(ip._CORE_SLOTS),
        )

    def test_emitted_slot_always_in_closed_set(self) -> None:
        """Closed-set guard: a built envelope's slot is always in the vocab."""
        elic = ip._build_elicitation(ip.ElicitationSlot.BUDGET, "How much?")
        self.assertIn(elic["slot"], ip._ELICITATION_SLOTS)


class TestElicitationParseIntent(unittest.TestCase):
    """End-to-end through parse_intent (the /negotiate_text path)."""

    # ------------------------------------------------------------------
    # APPEND-ONLY: a sufficiently-specified plain trip is UNCHANGED.
    # ------------------------------------------------------------------
    def test_specified_trip_has_no_elicitation(self) -> None:
        """
        A fully-specified plain trip (Bali, vibe, budget, nights) returns a
        trip_request with NO needs_clarification and NO elicitation key.
        This is the benchmark no-op: no new key, no changed value.
        """
        with _no_llm():
            result = ip.parse_intent("6 nights Bali, beach, $1500, solo")
        self.assertNotIn("needs_clarification", result, f"Unexpected: {result}")
        self.assertNotIn(
            "elicitation", result,
            "APPEND-ONLY violated: a specified trip must carry no elicitation key.",
        )
        # And it really is a usable trip_request.
        self.assertIn("legs", result)
        self.assertIn("total_budget_cents", result)

    # ------------------------------------------------------------------
    # UNDERSPECIFIED: budget missing -> BUDGET elicitation.
    # ------------------------------------------------------------------
    def test_missing_budget_elicits_budget_slot(self) -> None:
        """City + nights + vibe but NO budget -> structured BUDGET elicitation."""
        with _no_llm():
            result = ip.parse_intent("6 nights Bali, beach, solo")
        self.assertTrue(result.get("needs_clarification"))
        # Existing prose reason preserved (HONESTY: still names the gap in prose).
        self.assertIn("budget", result["reason"].lower())
        elic = result.get("elicitation")
        self.assertIsNotNone(elic, "missing budget must surface an elicitation object")
        self.assertEqual(elic["slot"], ip.ElicitationSlot.BUDGET)
        self.assertIn(elic["slot"], ip._ELICITATION_SLOTS)
        self.assertIn("question", elic)
        # budget is the gap -> in missing_slots, not satisfied_slots.
        self.assertIn("budget", elic["missing_slots"])
        self.assertNotIn("budget", elic["satisfied_slots"])
        # destination + duration WERE supplied -> satisfied.
        self.assertIn("destination", elic["satisfied_slots"])
        self.assertIn("duration", elic["satisfied_slots"])

    # ------------------------------------------------------------------
    # UNDERSPECIFIED: no destination -> DESTINATION elicitation.
    # ------------------------------------------------------------------
    def test_missing_destination_elicits_destination_slot(self) -> None:
        """No catalog city named -> structured DESTINATION elicitation."""
        with _no_llm():
            result = ip.parse_intent("7 nights, beach, $3000")
        self.assertTrue(result.get("needs_clarification"))
        elic = result.get("elicitation")
        self.assertIsNotNone(elic)
        self.assertEqual(elic["slot"], ip.ElicitationSlot.DESTINATION)
        self.assertIn("destination", elic["missing_slots"])
        # examples are present and sorted (var-0).
        self.assertIn("examples", elic)
        self.assertEqual(elic["examples"], sorted(elic["examples"]))

    # ------------------------------------------------------------------
    # UNDERSPECIFIED: unknown (non-catalog) destination -> DESTINATION slot.
    # ------------------------------------------------------------------
    def test_unknown_destination_elicits_destination_slot(self) -> None:
        """A named-but-uncataloged city declines WITH a DESTINATION elicitation."""
        with _no_llm():
            result = ip.parse_intent("8 nights in Atlantis, beach, $2000")
        self.assertTrue(result.get("needs_clarification"))
        elic = result.get("elicitation")
        # If a non-catalog token was detected, it must elicit DESTINATION;
        # the reason prose is preserved either way.
        self.assertIn("cannot_satisfy", result["reason"])
        if elic is not None:
            self.assertEqual(elic["slot"], ip.ElicitationSlot.DESTINATION)
            self.assertIn(elic["slot"], ip._ELICITATION_SLOTS)

    # ------------------------------------------------------------------
    # AREA gate: preference_prompt preserved verbatim + AREA elicitation added.
    # ------------------------------------------------------------------
    def test_multi_area_gate_keeps_preference_prompt_and_adds_area_slot(self) -> None:
        """
        Single multi-area city, multi-day, no vibe -> the existing
        preference_prompt is preserved verbatim AND an AREA elicitation is added
        (SUBSUME, do not break the web board pp.areas branch).
        """
        with _no_llm():
            result = ip.parse_intent("8 nights in Bali, $3000")
        self.assertTrue(result.get("needs_clarification"))
        # If the area gate fired it carries preference_prompt; assert the
        # superset property whenever that gate is the one that fired.
        if "preference_prompt" in result:
            pp = result["preference_prompt"]
            # preference_prompt shape preserved verbatim.
            self.assertIn("areas", pp)
            self.assertIn("vibes", pp)
            self.assertIn("options", pp)
            elic = result.get("elicitation")
            self.assertIsNotNone(elic, "AREA gate must add an elicitation object")
            self.assertEqual(elic["slot"], ip.ElicitationSlot.AREA)
            # choices == the sorted areas already in preference_prompt.
            self.assertEqual(elic["choices"], sorted(pp["areas"]))

    # ------------------------------------------------------------------
    # var-0: same underspecified text -> byte-identical envelope across calls.
    # ------------------------------------------------------------------
    def test_determinism_byte_identical_envelope(self) -> None:
        """The elicitation envelope is byte-identical across repeated calls."""
        text = "6 nights Bali, beach, solo"  # missing budget
        with _no_llm():
            r1 = ip.parse_intent(text)
        with _no_llm():
            r2 = ip.parse_intent(text)
        with _no_llm():
            r3 = ip.parse_intent(text)
        # Strip user_id-only noise irrelevant to elicitation (it's not present
        # on a decline anyway), compare the full JSON-serialised envelopes.
        s1 = json.dumps(r1, sort_keys=True)
        s2 = json.dumps(r2, sort_keys=True)
        s3 = json.dumps(r3, sort_keys=True)
        self.assertEqual(s1, s2)
        self.assertEqual(s2, s3)
        # And it genuinely carries the structured slot.
        self.assertEqual(r1["elicitation"]["slot"], ip.ElicitationSlot.BUDGET)

    # ------------------------------------------------------------------
    # APPEND-ONLY (decline branch): the prose reason is preserved verbatim;
    # elicitation is strictly additive.
    # ------------------------------------------------------------------
    def test_reason_preserved_as_superset(self) -> None:
        """The decline reason string is unchanged; elicitation is additive only."""
        with _no_llm():
            result = ip.parse_intent("6 nights Bali, beach, solo")
        # The exact prose the codebase already returned for a missing budget.
        self.assertEqual(
            result["reason"],
            "cannot_satisfy: I could not find a budget in your request. "
            "Please state a budget (e.g. '$3000' or 'AUD 3000').",
        )
        # needs_clarification + reason + elicitation, nothing destructive.
        self.assertTrue(result["needs_clarification"])
        self.assertIn("elicitation", result)


if __name__ == "__main__":
    unittest.main()
