"""test_aftercare_endpoint.py — #100 AFTERCARE: POST /aftercare/check HTTP endpoint tests.

Tests:
- EMERGENCY_FEED=stub, TELEGRAM_BOT_TOKEN="SECRET-TOKEN-123"
- Book a TW trip → POST /aftercare/check → 200, outcome:"ok", ≥1 high alert
- Assert "SECRET-TOKEN-123" not in json.dumps(body)
- Assert no token/telegram_token/chat_id key anywhere in the response
- not_booked for a plan_ready key
- unknown_trip for a bogus key

SECURITY (auth-session hardening, 2026-07 security review finding #1, MEDIUM): the
ownership gate used to be a bare `body['user_id'] == row['user_id']` equality
check — trivially spoofable, no proof of session possession required. It now
reuses server.py's `_authorize_trip_action` (the SAME two-tier session_token/
owner_token gate as /confirm /cancel /replan /refine). The tests below were
updated to present a real session_token (via POST /session/login for a
pre-seeded demo user) or owner_token instead of a bare claimed user_id.
"""
from __future__ import annotations

import json
import os

import pytest
from starlette.testclient import TestClient

# ─── app setup ───────────────────────────────────────────────────────────────

_ANON_TOKEN = "anon-secret-aftercare-A"


def _login(client: TestClient, user_id: str) -> str:
    """POST /session/login for a KNOWN pre-seeded demo user_id -> its session_token.
    Mirrors test_confirm_endpoint.py's `_login` helper."""
    r = client.post("/session/login", json={"user_id": user_id})
    assert r.status_code == 200, r.text
    return r.json()["session_token"]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("EMERGENCY_FEED", "stub")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET-TOKEN-123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "")
    monkeypatch.setenv("PUBLIC_WEBAPP_BASE", "https://travel.guild")
    # Use in-memory store
    from orchestration.store import SqliteDashboardStore, set_store
    from orchestration.demo_users import seed_demo_users
    store = SqliteDashboardStore(":memory:")
    set_store(store)
    # Pre-seed the 5 demo users so POST /session/login (the session_token proof
    # _authorize_trip_action now requires for owned trips) resolves without
    # needing the app's lifespan to run.
    seed_demo_users(store)
    # Seed a booked TW trip
    tw_envelope = {
        "idempotency_key": "trip-tw-e2e",
        "outcome": "success",
        "payment_status": "charged",
        "booking_ref": "BK-TW-001",
        "legs": [{
            "leg_id": "leg-0",
            "city": "Kaohsiung",
            "iso2": "TW",
            "checkin": "2026-09-12",
            "checkout": "2026-09-15",
        }],
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
    }
    # Save as plan_ready first
    store.save_plan({
        "idempotency_key": "trip-tw-e2e",
        "user_id": "demo-lena",
        "digest": "tw-e2e",
        "checkout_id": "co-tw-1",
        "package_total_cents": 50000,
        "envelope": tw_envelope,
    })
    # Then mark as booked
    store.mark_booked(
        "trip-tw-e2e",
        booking_ref="BK-TW-001",
        envelope=tw_envelope,
        confirmed_at="2026-09-01T10:00:00+00:00",
    )

    # Save a plan_ready (not booked) trip
    plan_envelope = {
        "idempotency_key": "trip-plan-only",
        "outcome": "plan_ready",
        "payment_status": "held",
        "legs": [{"leg_id": "leg-0", "city": "Tokyo", "iso2": "JP",
                  "checkin": "2026-10-01", "checkout": "2026-10-05"}],
        "day_plans": [{"leg_id": "leg-0", "city": "Tokyo", "iso2": "JP",
                       "checkin": "2026-10-01", "checkout": "2026-10-05",
                       "days": []}],
    }
    store.save_plan({
        "idempotency_key": "trip-plan-only",
        "user_id": "demo-alex",
        "digest": "plan-only",
        "package_total_cents": 30000,
        "envelope": plan_envelope,
    })

    # Anon (owner_token-bound) booked trip — exercises the Tier-2 half of
    # _authorize_trip_action on this endpoint.
    anon_envelope = {
        "idempotency_key": "trip-anon-e2e",
        "outcome": "success",
        "payment_status": "charged",
        "booking_ref": "BK-ANON-001",
        "legs": [{"leg_id": "leg-0", "city": "Kaohsiung", "iso2": "TW",
                  "checkin": "2026-09-12", "checkout": "2026-09-15"}],
        "day_plans": [{"leg_id": "leg-0", "city": "Kaohsiung", "iso2": "TW",
                       "checkin": "2026-09-12", "checkout": "2026-09-15",
                       "days": [{"day_index": 0,
                                 "attractions": [{"id": 1, "name": "Dragon Tiger Tower"}],
                                 "fair_weather_attractions": [{"id": 99, "name": "Pier-2 Art Center"}]}]}],
    }
    store.save_plan({
        "idempotency_key": "trip-anon-e2e",
        "user_id": "",
        "owner_token": _ANON_TOKEN,
        "digest": "anon-e2e",
        "package_total_cents": 50000,
        "envelope": anon_envelope,
    })
    store.mark_booked(
        "trip-anon-e2e",
        booking_ref="BK-ANON-001",
        envelope=anon_envelope,
        confirmed_at="2026-09-01T10:00:00+00:00",
    )

    from orchestration.server import build_app
    app = build_app()
    return TestClient(app, raise_server_exceptions=True)


# ─── booked TW trip → high alert ─────────────────────────────────────────────

def test_booked_tw_ok(client):
    # SECURITY: owned trip now requires a valid session_token (was a bare user_id claim).
    token = _login(client, "demo-lena")
    resp = client.post("/aftercare/check", json={
        "idempotency_key": "trip-tw-e2e",
        "session_token": token,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "ok"
    assert len(body["alerts"]) >= 1
    assert body["alerts"][0]["severity_tier"] == "high"


# ─── token NEVER in response ──────────────────────────────────────────────────

def test_token_not_in_response(client):
    token = _login(client, "demo-lena")
    resp = client.post("/aftercare/check", json={
        "idempotency_key": "trip-tw-e2e",
        "session_token": token,
    })
    body_str = json.dumps(resp.json())
    assert "SECRET-TOKEN-123" not in body_str, (
        f"Token found in /aftercare/check response: {body_str[:200]}"
    )


# ─── no token/telegram_token/chat_id key in body ─────────────────────────────

def test_no_secret_key_in_response(client):
    token = _login(client, "demo-lena")
    resp = client.post("/aftercare/check", json={
        "idempotency_key": "trip-tw-e2e",
        "session_token": token,
    })
    body = resp.json()
    body_str = json.dumps(body).lower()
    for forbidden_key in ("telegram_token", "token", "chat_id"):
        # The 'telegram' dict in the response is allowed to have 'sent'/'note'/'channel'
        # but NEVER 'token' or 'chat_id'
        tg = body.get("telegram") or {}
        assert forbidden_key not in tg, (
            f"Forbidden key '{forbidden_key}' found in telegram response: {tg}"
        )


# ─── not_booked ───────────────────────────────────────────────────────────────

def test_plan_ready_not_booked(client):
    token = _login(client, "demo-alex")
    resp = client.post("/aftercare/check", json={
        "idempotency_key": "trip-plan-only",
        "session_token": token,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "not_booked"
    assert body["alerts"] == []


# ─── unknown_trip ─────────────────────────────────────────────────────────────

def test_unknown_trip(client):
    resp = client.post("/aftercare/check", json={"idempotency_key": "trip-bogus-xyz"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "unknown_trip"


# ─── missing idempotency_key → 400 ──────────────────────────────────────────

def test_missing_key_400(client):
    resp = client.post("/aftercare/check", json={})
    assert resp.status_code == 400


# ─── Telegram summary in response ────────────────────────────────────────────

def test_telegram_summary_present(client):
    token = _login(client, "demo-lena")
    resp = client.post("/aftercare/check", json={
        "idempotency_key": "trip-tw-e2e",
        "session_token": token,
    })
    body = resp.json()
    tg = body.get("telegram") or {}
    # Token was set but no chat_id resolved (empty TELEGRAM_ALLOWED_USERS) → not attempted
    assert "sent" in tg
    assert "attempted" in tg


# ─── ownership-gate regression tests (was VULN-AUTH-001; now session_token/owner_token) ──

def test_idor_wrong_session_token_denied(client):
    """A session_token that is genuinely valid but bound to a DIFFERENT demo user than
    the trip's owner -> 200 but outcome=unknown_trip (no existence oracle, data NOT
    leaked to unauthorized caller). Regression lock for the old bare-user_id gate,
    which this exact scenario would have bypassed by simply claiming user_id=demo-lena."""
    other_token = _login(client, "demo-mei")  # trip-tw-e2e is owned by demo-lena
    resp = client.post("/aftercare/check", json={
        "idempotency_key": "trip-tw-e2e",
        "session_token": other_token,
    })
    assert resp.status_code == 200
    body = resp.json()
    # Should return the unknown_trip shape (not 403, to avoid oracle)
    assert body["outcome"] == "unknown_trip"
    # Ensure no data leak: alerts should be empty
    assert body["alerts"] == []
    assert body["monitoring"]["status"] == "unavailable"


def test_idor_fabricated_session_token_denied(client):
    """A session_token that was never issued (fabricated) -> denied, same as a
    wrong-user token. Proves the gate checks real session possession, not just
    'some string was present'."""
    resp = client.post("/aftercare/check", json={
        "idempotency_key": "trip-tw-e2e",
        "session_token": "fabricated-not-a-real-token",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "unknown_trip"
    assert body["alerts"] == []


def test_idor_session_token_omitted_denied(client):
    """POST with NO session_token against an owned trip -> 200 but outcome=unknown_trip.
    The omit-path must NOT bypass the check for an owned trip."""
    resp = client.post("/aftercare/check", json={
        "idempotency_key": "trip-tw-e2e"
        # NO session_token field
    })
    assert resp.status_code == 200
    body = resp.json()
    # Should return unknown_trip (not ok with alerts)
    assert body["outcome"] == "unknown_trip"
    assert body["alerts"] == []


def test_owner_match_ok(client):
    """POST with the OWNER's valid session_token for an owned trip -> 200 and
    outcome=ok with alerts. Authorized owner still gets full data."""
    token = _login(client, "demo-lena")
    resp = client.post("/aftercare/check", json={
        "idempotency_key": "trip-tw-e2e",
        "session_token": token,
    })
    assert resp.status_code == 200
    body = resp.json()
    # Authorized owner should get ok outcome with alerts
    assert body["outcome"] == "ok"
    assert len(body["alerts"]) >= 1
    assert body["alerts"][0]["severity_tier"] == "high"


def test_anon_trip_correct_owner_token_succeeds(client):
    """Anonymous (Tier-2) booked trip + the correct owner_token -> 200 and outcome=ok."""
    resp = client.post("/aftercare/check", json={
        "idempotency_key": "trip-anon-e2e",
        "owner_token": _ANON_TOKEN,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "ok"
    assert len(body["alerts"]) >= 1


def test_anon_trip_wrong_owner_token_denied(client):
    """Anonymous (Tier-2) booked trip + a WRONG owner_token -> 200 but
    outcome=unknown_trip (no existence oracle)."""
    resp = client.post("/aftercare/check", json={
        "idempotency_key": "trip-anon-e2e",
        "owner_token": "not-the-right-secret",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "unknown_trip"
    assert body["alerts"] == []


def test_guest_trip_no_owner_check(client):
    """POST with no session_token/owner_token against a LEGACY guest-created trip
    (user_id='' AND no owner_token ever stored) -> 200 and ok. This is
    _authorize_trip_action's documented fail-open transient-rollout-state path for
    an anon row created before owner_token existed — key is the bearer capability."""
    from orchestration.store import get_store
    # Create a guest-created (no owner, no owner_token) booked trip
    store = get_store()
    guest_envelope = {
        "idempotency_key": "trip-guest-xyz",
        "outcome": "success",
        "payment_status": "charged",
        "booking_ref": "BK-GUEST-001",
        "legs": [{
            "leg_id": "leg-0",
            "city": "Kaohsiung",
            "iso2": "TW",
            "checkin": "2026-09-12",
            "checkout": "2026-09-15",
        }],
        "day_plans": [{
            "leg_id": "leg-0",
            "city": "Kaohsiung",
            "iso2": "TW",
            "checkin": "2026-09-12",
            "checkout": "2026-09-15",
            "days": [{"day_index": 0, "attractions": [], "fair_weather_attractions": []}],
        }],
    }
    # Save with empty user_id (guest) and no owner_token
    store.save_plan({
        "idempotency_key": "trip-guest-xyz",
        "user_id": "",  # Guest trip: no owner
        "digest": "guest-xyz",
        "package_total_cents": 50000,
        "envelope": guest_envelope,
    })
    # Mark as booked
    store.mark_booked(
        "trip-guest-xyz",
        booking_ref="BK-GUEST-001",
        envelope=guest_envelope,
        confirmed_at="2026-09-01T10:00:00+00:00",
    )

    # POST with no session_token/owner_token — should NOT be blocked
    resp = client.post("/aftercare/check", json={
        "idempotency_key": "trip-guest-xyz"
        # NO session_token / owner_token
    })
    assert resp.status_code == 200
    body = resp.json()
    # Guest trips (legacy, no owner_token ever stored) skip the ownership check
    assert body["outcome"] == "ok"
    assert len(body["alerts"]) >= 1
