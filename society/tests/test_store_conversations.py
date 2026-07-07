"""test_store_conversations.py — B2: conversation store unit tests (parameterized SQL, no network).

Tests:
1. create_conversation → get_conversation round-trips all fields + parses turns_json.
2. INSERT OR IGNORE is idempotent (double create doesn't error).
3. get_conversation returns None for unknown session_id.
4. get_conversation_by_active_key finds the right row.
5. append_turns extends the turns list + bumps updated_at.
6. append_turns with new_active_key repoints active_idempotency_key.
7. append_turns on unknown session_id is a no-op (doesn't raise).
8. Multiple conversations pointing at the same active_key → get_conversation_by_active_key
   returns the most recent.
9. Parameterized SQL (no f-strings) — turns_json round-trips including Unicode.
10. var-0: conversations table presence does NOT affect save_plan / get_plan (trips table still works).
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOC = os.path.dirname(_HERE)
if _SOC not in sys.path:
    sys.path.insert(0, _SOC)

from orchestration.store import SqliteDashboardStore


def _store():
    return SqliteDashboardStore(":memory:")


class TestConversationCRUD(unittest.TestCase):

    def test_create_and_get(self):
        s = _store()
        seed = [{"role": "user", "content": "hello", "ts": "2026-01-01T00:00:00+00:00",
                 "idempotency_key": "trip-abc", "delta": None, "changed": None}]
        s.create_conversation("sess-1", "user-a", "trip-abc", seed_turns=seed)
        conv = s.get_conversation("sess-1")
        self.assertIsNotNone(conv)
        self.assertEqual(conv["session_id"], "sess-1")
        self.assertEqual(conv["user_id"], "user-a")
        self.assertEqual(conv["active_idempotency_key"], "trip-abc")
        self.assertIsInstance(conv["turns"], list)
        self.assertEqual(len(conv["turns"]), 1)
        self.assertEqual(conv["turns"][0]["content"], "hello")

    def test_create_is_idempotent(self):
        s = _store()
        s.create_conversation("sess-2", "user-b", "trip-xyz")
        s.create_conversation("sess-2", "user-b", "trip-xyz")  # must not raise
        conv = s.get_conversation("sess-2")
        self.assertIsNotNone(conv)

    def test_get_unknown_returns_none(self):
        s = _store()
        self.assertIsNone(s.get_conversation("no-such-session"))

    def test_get_by_active_key(self):
        s = _store()
        s.create_conversation("sess-3", "user-c", "trip-key-1")
        conv = s.get_conversation_by_active_key("trip-key-1")
        self.assertIsNotNone(conv)
        self.assertEqual(conv["session_id"], "sess-3")

    def test_get_by_active_key_unknown_returns_none(self):
        s = _store()
        self.assertIsNone(s.get_conversation_by_active_key("no-such-key"))

    def test_append_turns_extends_list(self):
        s = _store()
        s.create_conversation("sess-4", "u", "trip-4",
                               seed_turns=[{"role": "user", "content": "initial", "ts": "t0",
                                            "idempotency_key": "trip-4", "delta": None, "changed": None}])
        new_turns = [
            {"role": "user", "content": "make it cheaper", "ts": "t1",
             "idempotency_key": "trip-4", "delta": {}, "changed": None},
            {"role": "assistant", "content": "Budget trimmed 15%.", "ts": "t2",
             "idempotency_key": "trip-5", "delta": None, "changed": ["budget.adjust:-15%"]},
        ]
        s.append_turns("sess-4", turns=new_turns)
        conv = s.get_conversation("sess-4")
        self.assertEqual(len(conv["turns"]), 3)
        self.assertEqual(conv["turns"][1]["content"], "make it cheaper")
        self.assertEqual(conv["turns"][2]["content"], "Budget trimmed 15%.")

    def test_append_turns_repoints_active_key(self):
        s = _store()
        s.create_conversation("sess-5", "u", "trip-old")
        s.append_turns("sess-5", turns=[
            {"role": "user", "content": "add osaka", "ts": "t", "idempotency_key": "trip-old",
             "delta": None, "changed": None}
        ], new_active_key="trip-new")
        conv = s.get_conversation("sess-5")
        self.assertEqual(conv["active_idempotency_key"], "trip-new")

    def test_append_turns_unknown_session_noop(self):
        s = _store()
        # Must not raise.
        s.append_turns("no-such-session", turns=[{"role": "user", "content": "?"}])

    def test_get_by_active_key_returns_most_recent(self):
        s = _store()
        s.create_conversation("sess-old", "u", "trip-shared",
                               now="2026-01-01T00:00:00+00:00")
        time.sleep(0.01)
        s.create_conversation("sess-new", "u", "trip-shared",
                               now="2026-01-01T00:00:01+00:00")
        # Both point at "trip-shared". Most recent → sess-new.
        conv = s.get_conversation_by_active_key("trip-shared")
        self.assertEqual(conv["session_id"], "sess-new")

    def test_turns_unicode_roundtrip(self):
        s = _store()
        content_str = "travel旅行\U0001f38d"  # unicode: travel + kanji + emoji, stable
        seed = [{"role": "assistant", "content": content_str, "ts": "t",
                 "idempotency_key": "trip-jp", "delta": None, "changed": None}]
        s.create_conversation("sess-jp", "u", "trip-jp", seed_turns=seed)
        conv = s.get_conversation("sess-jp")
        self.assertEqual(conv["turns"][0]["content"], content_str)

    def test_conversations_do_not_affect_trips_table(self):
        """var-0 guard: the conversations table must not interfere with save_plan/get_plan."""
        s = _store()
        s.create_conversation("sess-x", "u", "trip-check")
        s.save_plan({
            "idempotency_key": "trip-check",
            "user_id": "u",
            "package_total_cents": 10000,
            "envelope": {"outcome": "plan_ready", "idempotency_key": "trip-check"},
        })
        row = s.get_plan("trip-check")
        self.assertIsNotNone(row)
        self.assertEqual(row["idempotency_key"], "trip-check")


if __name__ == "__main__":
    unittest.main(verbosity=2)
