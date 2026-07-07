"""test_aftercare_no_transaction.py — #100 AFTERCARE: critical channel-security test.

CRITICAL: asserts that the aftercare path performs NO booking/debit/cancel.

Tests:
- Seed a booked trip in an in-memory store; monkeypatch store.mark_booked,
  mark_cancelled, save_plan to raise AssertionError.
- Run run_aftercare_check with an active-TW stub feed + stub sender → no exception
  (none called); store.get_trip(idk)["status"]=="booked" + refunded_cents==0 (unchanged).
- Static guard: source of aftercare_monitor.py + telegram_notify.py + /aftercare/check
  handler in server.py assert none reference forbidden transactional symbols.
- Every alert's action_target.kind ∈ {replan_switch_variant, webpage_review, webpage_rebook}
  and any webpage_path is a webpage route (no transactional endpoint).
"""
from __future__ import annotations

import inspect
import re
import pytest

from utils.aftercare_monitor import run_aftercare_check
from utils.emergency_feed import stub_emergency_client


# ─── in-memory store with transactional guards ────────────────────────────────

class _GuardedStore:
    """Store that raises AssertionError if any transactional method is called."""

    def __init__(self, booked_trip: dict):
        self._trip = booked_trip
        self._users: dict = {}

    def get_trip(self, idk: str) -> dict | None:
        if idk == self._trip.get("idempotency_key"):
            return dict(self._trip)
        return None

    def get_user(self, uid: str) -> dict | None:
        return self._users.get(uid)

    # ─── FORBIDDEN — must never be called by aftercare ───────────────────────
    def mark_booked(self, *args, **kwargs):
        raise AssertionError("aftercare called mark_booked — CHANNEL SECURITY VIOLATION")

    def mark_cancelled(self, *args, **kwargs):
        raise AssertionError("aftercare called mark_cancelled — CHANNEL SECURITY VIOLATION")

    def save_plan(self, *args, **kwargs):
        raise AssertionError("aftercare called save_plan — CHANNEL SECURITY VIOLATION")


def _make_booked_tw_row() -> dict:
    return {
        "idempotency_key": "trip-sec-test",
        "digest": "sec-test",
        "status": "booked",
        "user_id": "demo-lena",
        "refunded_cents": 0,
        "envelope": {
            "idempotency_key": "trip-sec-test",
            "payment_status": "charged",
            "booking_ref": "BK-001",
            "legs": [{"leg_id": "leg-0", "city": "Kaohsiung", "iso2": "TW",
                      "checkin": "2026-09-12", "checkout": "2026-09-15"}],
            "day_plans": [{
                "leg_id": "leg-0",
                "city": "Kaohsiung",
                "iso2": "TW",
                "checkin": "2026-09-12",
                "checkout": "2026-09-15",
                "days": [{
                    "day_index": 0,
                    "attractions": [{"id": 1, "name": "Dragon Tiger Tower"}],
                    "fair_weather_attractions": [{"id": 99, "name": "Pier-2 Art Center"}],
                }],
            }],
        },
    }


def _stub_sender(chat_id, text, *, token="", allowed_users="", **kwargs):
    """Stub Telegram sender — records call but does nothing transactional."""
    return {"sent": False, "channel": "telegram", "note": "stub (test)"}


# ─── main no-transaction test ─────────────────────────────────────────────────

def test_aftercare_performs_no_transaction():
    """run_aftercare_check with active-TW stub → no transactional method called."""
    row = _make_booked_tw_row()
    store = _GuardedStore(booked_trip=row)

    # This must NOT raise AssertionError (which the guarded store would raise
    # if any transactional method is called)
    result = run_aftercare_check(
        "trip-sec-test", "demo-lena",
        store=store,
        emergency_client=stub_emergency_client,
        translator=None,
        sender=_stub_sender,
        webapp_base="https://travel.guild",
        now="2026-06-28",
    )
    # Outcome should be ok (active TW alert)
    assert result["outcome"] == "ok", f"Expected ok, got: {result['outcome']}"
    assert len(result["alerts"]) >= 1

    # State must be unchanged (the guarded store returns the original row)
    trip = store.get_trip("trip-sec-test")
    assert trip["status"] == "booked", "trip status must remain 'booked'"
    assert trip["refunded_cents"] == 0, "refunded_cents must remain 0"


# ─── static source guard ──────────────────────────────────────────────────────

FORBIDDEN_SYMBOLS = [
    "mark_booked",
    "mark_cancelled",
    "complete_checkout",
    "_wallet_debit",
    "cancel_checkout",
    "_commit",
    "_budget_commit",
]
# These endpoint paths must NOT appear as transaction targets in aftercare code
FORBIDDEN_ENDPOINT_CALLS = ["/confirm", "/cancel"]


def _get_source(module_name: str) -> str:
    import importlib
    mod = importlib.import_module(module_name)
    return inspect.getsource(mod)


def _has_call_site(src: str, sym: str) -> list[str]:
    """Return lines that have an actual call-site reference to sym (not just mentions
    in comments or docstrings). We look for `sym(` or `.sym(` patterns only."""
    call_patterns = [f"{sym}(", f".{sym}(", f"store.{sym}"]
    return [
        line for line in src.splitlines()
        if not line.strip().startswith("#") and any(p in line for p in call_patterns)
    ]


def test_aftercare_monitor_no_forbidden_symbols():
    src = _get_source("utils.aftercare_monitor")
    for sym in FORBIDDEN_SYMBOLS:
        call_lines = _has_call_site(src, sym)
        assert not call_lines, (
            f"Forbidden call-site '{sym}' found in aftercare_monitor.py: {call_lines}"
        )


def test_telegram_notify_no_forbidden_symbols():
    src = _get_source("utils.telegram_notify")
    for sym in FORBIDDEN_SYMBOLS:
        call_lines = _has_call_site(src, sym)
        assert not call_lines, (
            f"Forbidden call-site '{sym}' found in telegram_notify.py: {call_lines}"
        )


def test_server_aftercare_handler_no_forbidden_symbols():
    """The /aftercare/check handler in server.py must not reference forbidden symbols."""
    from orchestration import server
    src = inspect.getsource(server.aftercare_check)
    for sym in FORBIDDEN_SYMBOLS:
        call_lines = _has_call_site(src, sym)
        assert not call_lines, (
            f"Forbidden call-site '{sym}' found in aftercare_check handler: {call_lines}"
        )


# ─── alert action_target kinds ────────────────────────────────────────────────

ALLOWED_ACTION_TARGET_KINDS = {"replan_switch_variant", "webpage_review", "webpage_rebook"}
FORBIDDEN_PATHS = {"/confirm", "/cancel"}  # transactional endpoints


def test_alert_action_target_kinds():
    """All action_target.kind values must be in the allowed set."""
    row = _make_booked_tw_row()
    store = _GuardedStore(booked_trip=row)
    result = run_aftercare_check(
        "trip-sec-test", "",
        store=store,
        emergency_client=stub_emergency_client,
        sender=None,
    )
    for alert in result["alerts"]:
        at = alert.get("action_target")
        if at is None:
            # monitoring tier has no action_target — allowed
            assert alert.get("suggested_action") == "monitor"
            continue
        kind = at.get("kind")
        assert kind in ALLOWED_ACTION_TARGET_KINDS, (
            f"alert action_target.kind '{kind}' not in allowed set {ALLOWED_ACTION_TARGET_KINDS}"
        )
        # webpage_path must not point to a transactional endpoint
        wp = at.get("webpage_path") or ""
        for fp in FORBIDDEN_PATHS:
            assert not wp.startswith(fp) and fp not in wp, (
                f"webpage_path '{wp}' points to forbidden transactional endpoint '{fp}'"
            )
