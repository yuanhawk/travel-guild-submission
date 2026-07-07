"""test_replan_user_message_register.py — the human-register GATE for every
user-facing string that a replan op function can produce.

WHY THIS FILE EXISTS
---------------------
Task #198 fixed replan_ops.py's rejection reasons and _replan_notes entries,
which embedded raw 0-indexed leg_index/day_index values (and, on closer
inspection while building this gate, raw internal field-name jargon like
"leg_index"/"day_index"/"position"/"len="/"fair_weather_attractions"/
"meal_pool"/"day_a"/"day_b"/"restaurant_name") directly into strings that
reach the end user verbatim via POST /replan's response body (`rejected[]
.reason` and `notes[]`, rendered as-is by web/'s Itinerary.svelte
flash toast — see server.py's /replan handler and #198's commit message).

That was a point fix for the instances found at the time. THIS file is the
GATE that stops the same bug class from silently reappearing in a NEW or
future replan op: it drives every existing op function through every
reject/note-generating path, collects every user-facing string produced, and
asserts each one passes `assert_human_register()` — a reusable contract
(no underscores, no dev-jargon deny-list tokens, no 0-indexed "day N"/"leg N").

HOW TO EXTEND THIS GATE FOR A NEW REPLAN OP
---------------------------------------------
1. Add a case to `_collect_all_messages()` below that calls your new op
   function (or apply_ops with your new op type) through EVERY branch that
   returns a non-None reject reason, and every branch that appends to
   envelope["_replan_notes"]. Append each string produced to the returned list.
2. That's it — the existing `test_all_collected_messages_pass_human_register`
   test iterates whatever `_collect_all_messages()` returns and calls
   `assert_human_register()` on each one automatically. You do not need to
   write a new assertion per message; you only need to make sure your new
   op's reject/note paths are exercised and their output strings are
   collected into the list.

WHAT IS DELIBERATELY OUT OF SCOPE
-----------------------------------
The internal diff `tag` strings returned by each op function (e.g.
"remove:leg0.day0.pos0:Senso-ji") are NOT covered by this gate. They are a
machine-readable undo/tracking identifier, deliberately 0-indexed by design
(see replan_ops.py's module docstring and #198's commit message), and are
never rendered as prose to the user — web/'s Itinerary.svelte
only checks `r.applied.length`, it never joins/displays the tag strings
themselves (confirmed by reading Itinerary.svelte's flash-toast logic).
Only `rejected[].reason` and `notes[]` are prose shown to a human.
"""
from __future__ import annotations

import copy
import re
import unittest

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.replan_ops import (
    apply_ops,
    op_remove_place,
    op_add_place,
    op_swap_place,
    op_switch_variant,
    op_reflow_from,
    op_swap_meal,
    op_swap_days,
)

# ---------------------------------------------------------------------------
# The human-register contract
# ---------------------------------------------------------------------------

# Starter deny-list of internal/dev jargon tokens that must never leak into a
# user-facing string. Case-insensitive substring match. Extend this list as
# new jargon classes are discovered (mirrors the underscore check for
# identifier-shaped jargon that doesn't happen to contain '_', e.g. "len=").
_JARGON_DENYLIST = [
    "index",
    "closed set",
    "op-set",
    "envelope",
    "idempotency",
    "len=",
]

# "day 0" / "leg 0" (or any leg/day number < 1) is the exact 0-indexed-leak
# bug class #198 fixed — must never reappear. Matches "day N"/"leg N" with a
# real whitespace separator (so internal wire-format tokens like "day:1" in
# an echoed `from` spec do NOT trip this — those are opaque op-protocol
# values, not prose numbering, and are out of this gate's scope).
_DAY_LEG_NUMBER_RE = re.compile(r"\b(day|leg)\s+(\d+)\b", re.IGNORECASE)


def assert_human_register(message: str, *, context: str = "") -> None:
    """Assert `message` (a string that reaches the end user verbatim via
    /replan's `rejected[].reason` or `notes[]`) passes the human-register
    contract:

    1. No underscore characters — catches leg_index/day_index/day_a/day_b/
       restaurant_name/meal_pool/fair_weather_attractions-style raw
       identifier leaks (any literal internal field/variable name).
    2. No deny-listed dev-jargon token (see _JARGON_DENYLIST), case-insensitive.
    3. Any "day N" / "leg N" occurrence must have N >= 1 (never 0-indexed).

    Call this on EVERY user-facing string a replan op function can produce —
    every non-None reject reason and every _replan_notes entry.
    """
    label = f" ({context})" if context else ""
    assert isinstance(message, str) and message, (
        f"expected a non-empty user-facing string{label}, got {message!r}"
    )
    assert "_" not in message, (
        f"user-facing message contains a raw underscore-identifier leak{label}: {message!r}"
    )
    lowered = message.lower()
    for token in _JARGON_DENYLIST:
        assert token not in lowered, (
            f"user-facing message contains deny-listed jargon token {token!r}{label}: {message!r}"
        )
    for m in _DAY_LEG_NUMBER_RE.finditer(message):
        n = int(m.group(2))
        assert n >= 1, (
            f"user-facing message leaks a 0-indexed {m.group(1)} number{label}: {message!r}"
        )


# ---------------------------------------------------------------------------
# Shared fixture helpers (mirrors test_replan_endpoint.py's canned data —
# reused/paraphrased rather than duplicated wholesale; see that file's
# _make_envelope / _make_envelope_with_meals / _make_envelope_multiday for
# the full HTTP-level equivalents this file's pure-function-level fixtures
# are modeled on).
# ---------------------------------------------------------------------------

_ATT_A = {
    "name": "Senso-ji Temple", "name_en": "Senso-ji", "category": "temple",
    "wikidata": "Q844240", "lat": 35.71, "lon": 139.79, "provenance": "test",
}
_ATT_B = {
    "name": "Ueno Park", "name_en": "Ueno Park", "category": "park",
    "wikidata": None, "lat": 35.71, "lon": 139.77, "provenance": "test",
}
_ATT_C = {
    "name": "Tokyo Tower", "name_en": "Tokyo Tower", "category": "observation",
    "wikidata": None, "lat": 35.65, "lon": 139.74, "provenance": "test",
}
_ATT_E = {
    "name": "Meiji Shrine", "name_en": "Meiji Shrine", "category": "shrine",
    "wikidata": None, "lat": 35.67, "lon": 139.70, "provenance": "test",
}
_ATT_FAIR = {
    "name": "Shinjuku Gyoen", "name_en": "Shinjuku Gyoen", "category": "garden",
    "wikidata": None, "lat": 35.68, "lon": 139.71, "provenance": "test",
}


def _basic_envelope() -> dict:
    """1 leg, 2 days; day 0 = [A, C] + bad_weather+fair alt, day 1 = [].
    unscheduled = [B]."""
    return copy.deepcopy({
        "outcome": "plan_ready",
        "payment_status": "held",
        "booking_ref": None,
        "package_total_with_fees_cents": 150000,
        "wallet": {"debited": False},
        "day_plans": [
            {
                "days": [
                    {
                        "day_index": 0,
                        "bad_weather": True,
                        "attractions": [copy.deepcopy(_ATT_A), copy.deepcopy(_ATT_C)],
                        "fair_weather_attractions": [copy.deepcopy(_ATT_FAIR)],
                        "meals": {},
                    },
                    {
                        "day_index": 1,
                        "bad_weather": False,
                        "attractions": [],
                        "meals": {},
                    },
                ],
                "unscheduled_attractions": [copy.deepcopy(_ATT_B)],
            }
        ],
    })


def _multiday_envelope() -> dict:
    """5-day leg: day 0/4 boundary, days 1-3 interior. Day 2 is the only
    bad_weather=True day with a fair_weather_attractions alt (mirrors
    test_replan_endpoint.py's _make_envelope_multiday)."""
    return copy.deepcopy({
        "outcome": "plan_ready",
        "payment_status": "held",
        "booking_ref": None,
        "package_total_with_fees_cents": 150000,
        "wallet": {"debited": False},
        "day_plans": [
            {
                "days": [
                    {"day_index": 0, "bad_weather": False,
                     "attractions": [copy.deepcopy(_ATT_A)], "meals": {}},
                    {"day_index": 1, "bad_weather": False,
                     "attractions": [copy.deepcopy(_ATT_E)], "meals": {}},
                    {"day_index": 2, "bad_weather": True,
                     "attractions": [copy.deepcopy(_ATT_C)],
                     "fair_weather_attractions": [copy.deepcopy(_ATT_FAIR)], "meals": {}},
                    {"day_index": 3, "bad_weather": False,
                     "attractions": [copy.deepcopy(_ATT_B)], "meals": {}},
                    {"day_index": 4, "bad_weather": False,
                     "attractions": [copy.deepcopy(_ATT_A)], "meals": {}},
                ],
                "unscheduled_attractions": [],
            }
        ],
    })


def _meal_envelope() -> dict:
    env = _basic_envelope()
    day0 = env["day_plans"][0]["days"][0]
    day0["meals"] = {"lunch": {"name": "Sukiya", "cuisine": "gyudon"}}
    day0["meal_pool"] = {
        "lunch": [
            {"name": "Yoshinoya", "cuisine": "gyudon"},
            {"name": "Matsuya", "cuisine": "gyudon"},
        ]
    }
    return env


# ---------------------------------------------------------------------------
# Message collection: drive every op through every reject/note path.
# ---------------------------------------------------------------------------

def _collect_all_messages() -> list[tuple[str, str]]:
    """Return a list of (context_label, message) for every user-facing string
    (reject reason or _replan_notes entry) producible by the current set of
    replan op functions. See the module docstring for how to extend this for
    a new op."""
    out: list[tuple[str, str]] = []

    def _note(ctx: str, msg: str) -> None:
        out.append((ctx, msg))

    # ---- op_remove_place ----
    env = _basic_envelope()
    _, reason = op_remove_place(env, leg_index=9, day_index=0, position=0, attraction_ref="x")
    _note("remove_place: leg out of range", reason)

    env = _basic_envelope()
    _, reason = op_remove_place(env, leg_index=0, day_index=9, position=0, attraction_ref="x")
    _note("remove_place: day out of range", reason)

    env = _basic_envelope()
    _, reason = op_remove_place(env, leg_index=0, day_index=0, position=99, attraction_ref="x")
    _note("remove_place: position out of range", reason)

    env = _basic_envelope()
    _, reason = op_remove_place(env, leg_index=0, day_index=0, position=0, attraction_ref="Wrong Name")
    _note("remove_place: ref mismatch", reason)

    # ---- op_add_place ----
    env = _basic_envelope()
    _, reason = op_add_place(env, leg_index=9, day_index=0, position=0, attraction_ref="x")
    _note("add_place: leg out of range", reason)

    env = _basic_envelope()
    _, reason = op_add_place(env, leg_index=0, day_index=9, position=0, attraction_ref="x")
    _note("add_place: day out of range", reason)

    env = _basic_envelope()
    _, reason = op_add_place(env, leg_index=0, day_index=0, position=0, attraction_ref="GHOST")
    _note("add_place: ref not in pool", reason)

    # add_place pace-cap-exceeded note (_replan_notes)
    env = _basic_envelope()
    env["day_plans"][0]["days"][0]["attractions"] = [
        copy.deepcopy(_ATT_A), copy.deepcopy(_ATT_C), copy.deepcopy(_ATT_E),
    ]  # already at moderate cap (3)
    tag, reason = op_add_place(
        env, leg_index=0, day_index=0, position=0, attraction_ref="Ueno Park",
        from_spec="unscheduled", pace="moderate",
    )
    assert tag is not None, "fixture bug: add_place should have applied over-cap"
    for n in env.get("_replan_notes", []):
        _note("add_place: pace-cap-exceeded note", n)

    # ---- op_swap_place ----
    env = _basic_envelope()
    _, reason = op_swap_place(env, leg_index=9, day_index=0, position=0,
                                remove_ref="x", add_ref="y")
    _note("swap_place: leg out of range", reason)

    env = _basic_envelope()
    _, reason = op_swap_place(env, leg_index=0, day_index=9, position=0,
                                remove_ref="x", add_ref="y")
    _note("swap_place: day out of range", reason)

    env = _basic_envelope()
    _, reason = op_swap_place(env, leg_index=0, day_index=0, position=99,
                                remove_ref="x", add_ref="y")
    _note("swap_place: position out of range", reason)

    env = _basic_envelope()
    _, reason = op_swap_place(env, leg_index=0, day_index=0, position=0,
                                remove_ref="Wrong Name", add_ref="Ueno Park")
    _note("swap_place: remove_ref mismatch", reason)

    env = _basic_envelope()
    _, reason = op_swap_place(env, leg_index=0, day_index=0, position=0,
                                remove_ref="Senso-ji", add_ref="GHOST")
    _note("swap_place: add_ref not in pool", reason)

    # ---- op_switch_variant ----
    env = _basic_envelope()
    _, reason = op_switch_variant(env, leg_index=0, day_index=0, variant="sideways")
    _note("switch_variant: invalid variant", reason)

    env = _basic_envelope()
    _, reason = op_switch_variant(env, leg_index=9, day_index=0, variant="fair")
    _note("switch_variant: leg out of range", reason)

    env = _basic_envelope()
    _, reason = op_switch_variant(env, leg_index=0, day_index=9, variant="fair")
    _note("switch_variant: day out of range", reason)

    env = _basic_envelope()
    _, reason = op_switch_variant(env, leg_index=0, day_index=1, variant="fair")
    _note("switch_variant: no fair_weather_attractions (not a bad-weather day)", reason)

    env = _basic_envelope()
    _, reason = op_switch_variant(env, leg_index=0, day_index=0, variant="wet")
    _note("switch_variant: already showing that variant", reason)

    # ---- op_reflow_from ----
    env = _basic_envelope()
    _, reason = op_reflow_from(env, leg_index=9, from_day_index=0)
    _note("reflow_from: leg out of range", reason)

    env = _basic_envelope()
    _, reason = op_reflow_from(env, leg_index=0, from_day_index=99)
    _note("reflow_from: from_day_index out of range", reason)

    # ---- op_swap_meal ----
    # NOTE: the invalid-slot value below is deliberately underscore-free
    # ("teatime", not "tea_time") — `slot` is echoed back verbatim in the
    # rejection message (so the caller can see exactly what they sent),
    # which is honest client-input echoing, not a server-authored jargon
    # leak (same pattern as attraction_ref!r/remove_ref!r elsewhere in
    # replan_ops.py). Picking a garbage value that itself contains an
    # underscore would be testing the fixture's input, not the server's
    # message template — so this uses a clean value to isolate that.
    env = _meal_envelope()
    _, reason = op_swap_meal(env, leg_index=0, day_index=0, slot="teatime",
                               restaurant_name="Yoshinoya")
    _note("swap_meal: invalid slot", reason)

    env = _meal_envelope()
    _, reason = op_swap_meal(env, leg_index=0, day_index=0, slot="lunch",
                               restaurant_name="")
    _note("swap_meal: missing restaurant_name", reason)

    env = _meal_envelope()
    _, reason = op_swap_meal(env, leg_index=9, day_index=0, slot="lunch",
                               restaurant_name="Yoshinoya")
    _note("swap_meal: leg out of range", reason)

    env = _meal_envelope()
    _, reason = op_swap_meal(env, leg_index=0, day_index=9, slot="lunch",
                               restaurant_name="Yoshinoya")
    _note("swap_meal: day out of range", reason)

    env = _meal_envelope()
    _, reason = op_swap_meal(env, leg_index=0, day_index=0, slot="lunch",
                               restaurant_name="Made Up Place")
    _note("swap_meal: restaurant not found in pool", reason)

    # ---- op_swap_days ----
    env = _multiday_envelope()
    _, reason = op_swap_days(env, leg_index=0, day_a="not-a-number", day_b=1)
    _note("swap_days: non-integer day", reason)

    env = _multiday_envelope()
    _, reason = op_swap_days(env, leg_index=9, day_a=1, day_b=3)
    _note("swap_days: leg out of range", reason)

    env = _multiday_envelope()
    _, reason = op_swap_days(env, leg_index=0, day_a=1, day_b=99)
    _note("swap_days: day_b out of range", reason)

    env = _multiday_envelope()
    _, reason = op_swap_days(env, leg_index=0, day_a=99, day_b=1)
    _note("swap_days: day_a out of range", reason)

    env = _multiday_envelope()
    _, reason = op_swap_days(env, leg_index=0, day_a=2, day_b=2)
    _note("swap_days: same day", reason)

    env = _multiday_envelope()
    _, reason = op_swap_days(env, leg_index=0, day_a=0, day_b=1)
    _note("swap_days: boundary day", reason)

    # swap_days bad-weather-advisory-stays-with-date note (_replan_notes):
    # day_a=1 (fair) <-> day_b=2 (bad_weather=True) — triggers both the
    # "advisory stays with the date" note AND the "no rain-ready alternative"
    # note (day 2's slot keeps bad_weather=True but inherits day 1's content,
    # which has no fair_weather_attractions).
    env = _multiday_envelope()
    tag, reason = op_swap_days(env, leg_index=0, day_a=1, day_b=2)
    assert tag is not None, "fixture bug: swap_days should have applied"
    for n in env.get("_replan_notes", []):
        _note("swap_days: bad-weather/no-fair-alt notes", n)

    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReplanUserMessageRegister(unittest.TestCase):

    def test_assert_human_register_rejects_underscore_leak(self):
        with self.assertRaises(AssertionError):
            assert_human_register("leg_index 2 out of range")

    def test_assert_human_register_rejects_jargon_token(self):
        with self.assertRaises(AssertionError):
            assert_human_register("that index is closed set already")
        with self.assertRaises(AssertionError):
            assert_human_register("bad envelope state (len=3)")

    def test_assert_human_register_rejects_0_indexed_day_or_leg(self):
        with self.assertRaises(AssertionError):
            assert_human_register("day 0 (leg 0) now exceeds pace cap")
        with self.assertRaises(AssertionError):
            assert_human_register("leg 0 out of range")

    def test_assert_human_register_accepts_1_indexed_clean_message(self):
        # Must not raise.
        assert_human_register("day 2 (leg 1) now exceeds pace cap (4>3) after add — kept, flagged.")
        assert_human_register("position 3 out of range (day has 2 attractions)")

    def test_collector_gathers_a_nonzero_number_of_messages(self):
        """Sanity check on the harness itself: if this drops to 0, every op's
        reject paths silently stopped firing (fixture drift) and the gate
        below would vacuously pass — catch that here."""
        messages = _collect_all_messages()
        self.assertGreaterEqual(
            len(messages), 25,
            "expected at least one collected message per reject/note path across all "
            "7 op functions — got far fewer; a fixture likely stopped triggering its "
            "intended failure path (see _collect_all_messages)",
        )
        for ctx, msg in messages:
            self.assertIsNotNone(msg, f"{ctx}: expected a non-None reject reason/note")

    def test_all_collected_messages_pass_human_register(self):
        """THE GATE. Every reject reason / _replan_notes entry producible by
        the current replan ops must pass the human-register contract. See
        the module docstring for how a newly-added op's messages get folded
        into this same check."""
        messages = _collect_all_messages()
        failures: list[str] = []
        for ctx, msg in messages:
            try:
                assert_human_register(msg, context=ctx)
            except AssertionError as exc:
                failures.append(str(exc))
        self.assertEqual(
            failures, [],
            "one or more replan-op user-facing messages failed the human-register "
            "contract:\n" + "\n".join(failures),
        )

    def test_apply_ops_top_level_reject_reasons_also_pass(self):
        """apply_ops' own reject paths (malformed op / unknown op type) are a
        separate surface from the per-op-function reasons above — also
        user-facing via /replan's `rejected[].reason`."""
        env = _basic_envelope()
        _, rejected = apply_ops(env, [{"not": "a valid op shape, missing 'op' key"}])
        # Malformed (not a dict) case:
        _, rejected2 = apply_ops(env, ["not-a-dict"])
        for r in rejected + rejected2:
            assert_human_register(r["reason"], context=f"apply_ops rejected: op={r.get('op')!r}")


if __name__ == "__main__":
    unittest.main()
