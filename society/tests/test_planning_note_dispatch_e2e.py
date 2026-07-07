"""test_planning_note_dispatch_e2e.py — #195 PROD integration.

Exercises the REAL telegram_notify.send_telegram_alert (via its `_post` injection seam,
no network) driven by the REAL dispatch + REAL SqliteDashboardStore, end-to-end:

(1) A booked trip with an L4 planning_note produces exactly ONE Telegram send whose
    outbound payload is well-formed: HTML parse_mode, verbatim note text, a URL-button
    keyboard pointing at the trip webpage, and NO callback_data (channel-security).
(2) SEND-ONCE holds across a subsequent periodic aftercare poll: run_aftercare_check
    (the READ-ONLY monitor) is a SEPARATE code path that never sends the static note,
    and a second dispatch is 'already_sent' — so `_post` fires exactly once in total.
(3) The aftercare poll does not touch the send-once marker (read-only invariant intact).
"""
from __future__ import annotations

from functools import partial

from orchestration.store import SqliteDashboardStore
from utils.planning_note_dispatch import KIND, dispatch_planning_notes
from utils.telegram_notify import send_telegram_alert
from utils.aftercare_monitor import run_aftercare_check

_L4_NOTE = ("US State Dept Level 4 (Do Not Travel) — insurance coverage is not "
            "extended to armed conflict zones (EXC-WAR-2). This destination is "
            "outside current booking scope.")


def _booked_store(chat_id="555"):
    store = SqliteDashboardStore(":memory:")
    store.upsert_demo_user({"user_id": "u1", "display_name": "U",
                            "prefs": {"telegram_chat_id": chat_id}})
    env = {"outcome": "success", "risk_signals": {"per_leg": [
        {"leg_id": "leg-0", "city": "Phuket", "planning_note": None},
        {"leg_id": "leg-1", "city": "Kabul", "planning_note": _L4_NOTE},
    ]}}
    store.save_plan({"idempotency_key": "trip-abc", "user_id": "u1",
                     "digest": "abc", "envelope": env})
    store.mark_booked("trip-abc", booking_ref="BR-1", envelope=env,
                      confirmed_at="2026-07-03T00:00:00Z")
    return store


def test_e2e_real_formatter_payload_and_send_once():
    store = _booked_store()
    posted: list = []

    def fake_post(url, headers=None, json=None):
        posted.append({"url": url, "json": json})
        return {"ok": True, "result": {"message_id": 1}}

    # REAL telegram sender, only the HTTP POST is stubbed (via the documented _post seam)
    sender = partial(send_telegram_alert, _post=fake_post)

    r1 = dispatch_planning_notes(
        "trip-abc", "u1", store=store, sender=sender,
        webapp_base="https://tg.example", token="BOT-TOKEN", allowed_users="555",
    )
    assert r1["outcome"] == "sent"
    assert r1["telegram"]["sent"] is True
    assert store.was_notification_sent("trip-abc", KIND) is True

    # ── verify the actual outbound Telegram payload ──
    assert len(posted) == 1
    payload = posted[0]["json"]
    assert "BOT-TOKEN" in posted[0]["url"]         # token in URL (server-side only)
    assert payload["chat_id"] == "555"
    assert payload["parse_mode"] == "HTML"
    assert _L4_NOTE in payload["text"]             # verbatim, un-summarised
    assert "Kabul" in payload["text"]
    assert "Phuket" not in payload["text"]         # note-less leg excluded
    # URL-button keyboard → webpage only, NO callback_data (channel-security boundary)
    kb = payload["reply_markup"]["inline_keyboard"]
    btn = kb[0][0]
    assert btn["url"] == "https://tg.example/trip/trip-abc?focus=leg-1"
    assert "callback_data" not in btn
    assert "callback_data" not in repr(payload)

    # ── (2)+(3) a periodic aftercare poll neither re-sends nor touches the marker ──
    poll = run_aftercare_check(
        "trip-abc", "u1", store=store,
        emergency_client=None,        # no live feed → honest 'unavailable', zero alerts
        sender=sender, now="2026-07-04T00:00:00Z",
    )
    assert poll["telegram"]["sent"] is False       # poll path never sends the static note
    assert len(posted) == 1                        # still exactly one send in total
    assert store.was_notification_sent("trip-abc", KIND) is True  # marker untouched by poll

    # a later re-dispatch (any trigger) is a no-op: send-once holds
    r2 = dispatch_planning_notes("trip-abc", "u1", store=store, sender=sender,
                                 token="BOT-TOKEN", allowed_users="555")
    assert r2["outcome"] == "already_sent"
    assert len(posted) == 1


def test_server_wiring_gated_on_token(monkeypatch):
    """The /confirm booking-confirm seam (_maybe_dispatch_planning_note): fires the
    dispatch only when TELEGRAM_BOT_TOKEN is set (inert in UAT), reads the store, and
    delivers the note send-once. Exercises the real helper, not a reimplementation."""
    from orchestration import server

    store = _booked_store(chat_id="555")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "555")
    monkeypatch.setenv("PUBLIC_WEBAPP_BASE", "https://tg.example")

    # (a) No token → genuinely inert: never touches the store, never sends, never raises.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    class _Boom:  # any store access would blow up → proves the no-token path reads nothing
        def __getattr__(self, _):
            raise AssertionError("store must not be touched when TELEGRAM_BOT_TOKEN unset")

    server._maybe_dispatch_planning_note("trip-abc", "u1", _Boom())  # must be a silent no-op
    assert store.was_notification_sent("trip-abc", KIND) is False

    # (b) Token set → dispatch runs against the real store and delivers exactly once.
    # Stub only the final network sender (helper injects no sender → uses _default_sender,
    # which imports this symbol at call time), keeping the rest of the path real + offline.
    import utils.telegram_notify as tn
    monkeypatch.setattr(tn, "send_telegram_alert",
                        lambda chat_id, text, **kw: {"sent": True, "channel": "telegram", "note": "ok"})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOT-TOKEN")
    server._maybe_dispatch_planning_note("trip-abc", "u1", store)
    assert store.was_notification_sent("trip-abc", KIND) is True
    # idempotent: a second confirm-time fire does not re-send (send-once holds)
    server._maybe_dispatch_planning_note("trip-abc", "u1", store)
    assert store.was_notification_sent("trip-abc", KIND) is True


def test_e2e_allowlist_rejection_is_not_marked():
    """Real sender's allowlist gate rejects a non-listed chat_id → send_failed, NOT marked
    (so fixing the allowlist and retrying still delivers)."""
    store = _booked_store(chat_id="555")
    posted: list = []
    sender = partial(send_telegram_alert,
                     _post=lambda *a, **k: posted.append(1) or {"ok": True})
    r = dispatch_planning_notes("trip-abc", "u1", store=store, sender=sender,
                                token="BOT-TOKEN", allowed_users="111,222")  # 555 not listed
    assert r["outcome"] == "send_failed"
    assert posted == []                            # real sender short-circuits before POST
    assert store.was_notification_sent("trip-abc", KIND) is False
