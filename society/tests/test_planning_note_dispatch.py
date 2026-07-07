"""test_planning_note_dispatch.py — #195 PROD: one-time planning_note Telegram push.

Unit coverage:
(a) store send-once marker: round-trip + per-(trip,kind) isolation + idempotent re-mark
(b) extract_planning_notes: VERBATIM text, skips None/non-str, empty on missing risk_signals
(c) formatter: HTML-escapes, includes verbatim note, excludes note-less legs
(d) send-once: first dispatch sends, second is 'already_sent' (sender called exactly once)
(e) no planning notes → 'no_planning_notes', NOT marked (harmless re-eval)
(f) not booked / unknown trip → no-op outcomes
(g) no token → 'telegram_disabled', NOT marked (a configured retry can still deliver)
(h) token but no chat_id → 'no_channel', NOT marked → a later subscribe delivers it
(i) sender reports not-sent → 'send_failed', NOT marked → retry succeeds and marks
(j) chat_id resolves from TELEGRAM_ALLOWED_USERS env when user has no pref

Uses the REAL SqliteDashboardStore so risk_signals survives the JSON persistence round-trip.
"""
from __future__ import annotations

import pytest

from orchestration.store import SqliteDashboardStore
from utils import planning_note_dispatch as pnd
from utils.planning_note_dispatch import (
    KIND,
    build_planning_note_message,
    dispatch_planning_notes,
    extract_planning_notes,
)

_L4_NOTE = ("US State Dept Level 4 (Do Not Travel) — reconsider need to travel. "
            "This destination is outside current booking scope.")
_L3_NOTE = ("US State Dept Level 3 (Reconsider Travel) — travel is generally "
            "manageable with advance planning and a reputable local guide.")


# ─── helpers ────────────────────────────────────────────────────────────────

def _envelope(per_leg: list[dict]) -> dict:
    return {"outcome": "success", "risk_signals": {"consolidator": "risk-agent",
                                                    "per_leg": per_leg}}


def _seed_booked(store, *, key="trip-abc", user_id="u1", per_leg=None,
                 chat_id: str | None = None) -> None:
    """Seed a booked trip (plan_ready → mark_booked) with a risk_signals per_leg block,
    plus (optionally) a demo user carrying a telegram_chat_id pref."""
    per_leg = per_leg if per_leg is not None else [
        {"leg_id": "leg-0", "city": "Bali", "planning_note": None},
        {"leg_id": "leg-1", "city": "Kabul", "planning_note": _L4_NOTE},
    ]
    if chat_id is not None:
        store.upsert_demo_user({"user_id": user_id, "display_name": "U",
                                "prefs": {"telegram_chat_id": chat_id}})
    store.save_plan({"idempotency_key": key, "user_id": user_id, "digest": "abc",
                     "envelope": _envelope(per_leg)})
    store.mark_booked(key, booking_ref="BR-1", envelope=_envelope(per_leg),
                      confirmed_at="2026-07-03T00:00:00Z")


class _Sender:
    """Capturing fake sender matching send_telegram_alert's signature/return."""

    def __init__(self, sent: bool = True, note: str = "ok"):
        self.calls: list = []
        self._sent = sent
        self._note = note

    def __call__(self, chat_id, text, **kwargs):
        self.calls.append((chat_id, text, kwargs))
        return {"sent": self._sent, "channel": "telegram", "note": self._note}


# ─── (a) store send-once marker ──────────────────────────────────────────────

def test_store_marker_roundtrip_and_isolation():
    store = SqliteDashboardStore(":memory:")
    assert store.was_notification_sent("trip-x", KIND) is False
    store.mark_notification_sent("trip-x", KIND)
    assert store.was_notification_sent("trip-x", KIND) is True
    # per-kind and per-trip isolation
    assert store.was_notification_sent("trip-x", "other") is False
    assert store.was_notification_sent("trip-y", KIND) is False
    # idempotent re-mark never raises / never flips
    store.mark_notification_sent("trip-x", KIND)
    assert store.was_notification_sent("trip-x", KIND) is True


# ─── (b) extract_planning_notes ──────────────────────────────────────────────

def test_extract_verbatim_and_skips_none():
    trip = {"envelope": _envelope([
        {"leg_id": "leg-0", "city": "Bali", "planning_note": None},
        {"leg_id": "leg-1", "city": "Kabul", "planning_note": _L4_NOTE},
        {"leg_id": "leg-2", "city": "Nowhere", "planning_note": ""},   # empty skipped
        {"leg_id": "leg-3", "city": "X", "planning_note": 123},        # non-str skipped
    ])}
    notes = extract_planning_notes(trip)
    assert len(notes) == 1
    assert notes[0]["city"] == "Kabul"
    assert notes[0]["planning_note"] == _L4_NOTE  # VERBATIM, byte-identical
    assert notes[0]["leg_index"] == 1


def test_extract_empty_on_missing_risk_signals():
    assert extract_planning_notes({"envelope": {"outcome": "success"}}) == []
    assert extract_planning_notes({}) == []
    assert extract_planning_notes({"envelope": {"risk_signals": {}}}) == []


# ─── (c) formatter ───────────────────────────────────────────────────────────

def test_formatter_verbatim_and_escapes():
    notes = [{"city": "Kabul & <b>Zone</b>", "planning_note": _L4_NOTE, "leg_index": 0}]
    msg = build_planning_note_message(notes)
    assert _L4_NOTE in msg                      # note text verbatim
    assert "Kabul &amp; &lt;b&gt;Zone&lt;/b&gt;" in msg  # city HTML-escaped (no injection)
    assert "Trip Planning Guidance" in msg


# ─── (d) send-once ───────────────────────────────────────────────────────────

def test_dispatch_sends_once():
    store = SqliteDashboardStore(":memory:")
    _seed_booked(store, chat_id="555")
    sender = _Sender()

    r1 = dispatch_planning_notes("trip-abc", "u1", store=store, sender=sender,
                                 webapp_base="https://tg.io", token="TOK", allowed_users="")
    assert r1["outcome"] == "sent"
    assert r1["notes_count"] == 1
    assert store.was_notification_sent("trip-abc", KIND) is True

    r2 = dispatch_planning_notes("trip-abc", "u1", store=store, sender=sender,
                                 token="TOK", allowed_users="")
    assert r2["outcome"] == "already_sent"
    assert len(sender.calls) == 1  # exactly one delivery across two triggers

    # message + URL button target are correct (leg-1 focus, absolute webapp base)
    chat_id, text, kwargs = sender.calls[0]
    assert chat_id == "555"
    assert "Kabul" in text and "Bali" not in text
    assert kwargs["webpage_url"] == "https://tg.io/trip/trip-abc?focus=leg-1"


# ─── (e) no planning notes → not marked ──────────────────────────────────────

def test_dispatch_no_notes_not_marked():
    store = SqliteDashboardStore(":memory:")
    _seed_booked(store, chat_id="555", per_leg=[
        {"leg_id": "leg-0", "city": "Bali", "planning_note": None}])
    sender = _Sender()
    r = dispatch_planning_notes("trip-abc", "u1", store=store, sender=sender, token="TOK")
    assert r["outcome"] == "no_planning_notes"
    assert sender.calls == []
    assert store.was_notification_sent("trip-abc", KIND) is False


# ─── (f) not booked / unknown ────────────────────────────────────────────────

def test_dispatch_not_booked():
    store = SqliteDashboardStore(":memory:")
    store.save_plan({"idempotency_key": "trip-abc", "user_id": "u1",
                     "envelope": _envelope([{"leg_id": "leg-0", "city": "Kabul",
                                             "planning_note": _L4_NOTE}])})
    sender = _Sender()
    r = dispatch_planning_notes("trip-abc", "u1", store=store, sender=sender, token="TOK")
    assert r["outcome"] == "not_booked"
    assert sender.calls == []


def test_dispatch_unknown_trip():
    store = SqliteDashboardStore(":memory:")
    r = dispatch_planning_notes("trip-nope", "u1", store=store, sender=_Sender(), token="TOK")
    assert r["outcome"] == "unknown_trip"


# ─── (g) no token → disabled, not marked ─────────────────────────────────────

def test_dispatch_no_token_disabled_not_marked(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    store = SqliteDashboardStore(":memory:")
    _seed_booked(store, chat_id="555")
    sender = _Sender()
    r = dispatch_planning_notes("trip-abc", "u1", store=store, sender=sender, token="")
    assert r["outcome"] == "telegram_disabled"
    assert sender.calls == []
    assert store.was_notification_sent("trip-abc", KIND) is False


# ─── (h) token but no channel → not marked, later subscribe delivers ─────────

def test_dispatch_no_channel_then_later_subscribe_delivers():
    store = SqliteDashboardStore(":memory:")
    _seed_booked(store, chat_id=None)  # user has no telegram_chat_id pref
    sender = _Sender()
    # booking-confirm trigger: no channel yet → NOT marked
    r1 = dispatch_planning_notes("trip-abc", "u1", store=store, sender=sender,
                                 token="TOK", allowed_users="")
    assert r1["outcome"] == "no_channel"
    assert store.was_notification_sent("trip-abc", KIND) is False
    assert sender.calls == []
    # user later links Telegram → subscribe-time trigger delivers exactly once
    store.upsert_demo_user({"user_id": "u1", "display_name": "U",
                            "prefs": {"telegram_chat_id": "777"}})
    r2 = dispatch_planning_notes("trip-abc", "u1", store=store, sender=sender,
                                 token="TOK", allowed_users="")
    assert r2["outcome"] == "sent"
    assert store.was_notification_sent("trip-abc", KIND) is True
    assert len(sender.calls) == 1


# ─── (i) send failure → not marked, retry succeeds ───────────────────────────

def test_dispatch_send_failed_not_marked_then_retry():
    store = SqliteDashboardStore(":memory:")
    _seed_booked(store, chat_id="555")
    fail = _Sender(sent=False, note="api error: boom")
    r1 = dispatch_planning_notes("trip-abc", "u1", store=store, sender=fail, token="TOK")
    assert r1["outcome"] == "send_failed"
    assert store.was_notification_sent("trip-abc", KIND) is False
    # retry with a working sender delivers and marks
    ok = _Sender(sent=True)
    r2 = dispatch_planning_notes("trip-abc", "u1", store=store, sender=ok, token="TOK")
    assert r2["outcome"] == "sent"
    assert store.was_notification_sent("trip-abc", KIND) is True


def test_dispatch_sender_raises_is_swallowed():
    store = SqliteDashboardStore(":memory:")
    _seed_booked(store, chat_id="555")

    def boom(*a, **k):
        raise RuntimeError("network down")

    r = dispatch_planning_notes("trip-abc", "u1", store=store, sender=boom, token="TOK")
    assert r["outcome"] == "send_failed"
    assert r["telegram"]["attempted"] is True
    assert store.was_notification_sent("trip-abc", KIND) is False


# ─── (j) chat_id from TELEGRAM_ALLOWED_USERS env fallback ─────────────────────

def test_dispatch_chat_from_allowed_users_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "999,888")
    store = SqliteDashboardStore(":memory:")
    _seed_booked(store, chat_id=None)  # no user pref → falls back to env allowlist[0]
    sender = _Sender()
    r = dispatch_planning_notes("trip-abc", "u1", store=store, sender=sender, token="TOK")
    assert r["outcome"] == "sent"
    assert sender.calls[0][0] == "999"  # first allowlist entry
