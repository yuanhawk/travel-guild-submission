"""test_aftercare_monitor.py — #100 AFTERCARE: monitor_booked_trip + run_aftercare_check tests.

Tests:
(a) booked TW trip + stub_emergency_client → exactly one severity_tier:"high" tropical_cyclone alert
(b) Day carries fair_weather_attractions → suggested_action=="switch_wet_weather_variant" with valid replan_op
(c) No variant → suggested_action=="reconsider_leg"
(d) JP stub monitoring → severity_tier:"monitoring", suggested_action:"monitor", no action_target
(e) emergency_client=None → monitoring.status=="unavailable", alerts==[], beta_note set (no fabricated all-clear)
(f) Multi-city same-country dedup (one alert per iso2)
(g) Missing iso2 leg → beta True + in beta_legs
"""
from __future__ import annotations

import pytest

from utils.emergency_feed import stub_emergency_client
from utils.aftercare_monitor import monitor_booked_trip, run_aftercare_check


# ─── helpers ────────────────────────────────────────────────────────────────

def _make_trip_row(
    iso2: str = "TW",
    city: str = "Kaohsiung",
    status: str = "booked",
    with_fair_weather: bool = True,
    extra_plans: list | None = None,
) -> dict:
    """Build a minimal trip row suitable for monitor_booked_trip."""
    day = {
        "day_index": 0,
        "attractions": [{"id": 1, "name": "Dragon Tiger Tower"}],
    }
    if with_fair_weather:
        day["fair_weather_attractions"] = [{"id": 99, "name": "Pier-2 Art Center"}]

    plan: dict = {
        "leg_id": "leg-0",
        "city": city,
        "iso2": iso2,
        "checkin": "2026-09-12",
        "checkout": "2026-09-15",
        "days": [day],
    }
    plans = [plan] + (extra_plans or [])

    legs = [
        {
            "leg_id": "leg-0",
            "city": city,
            "checkin": "2026-09-12",
            "checkout": "2026-09-15",
        }
    ]
    if extra_plans:
        for i, ep in enumerate(extra_plans, 1):
            legs.append({
                "leg_id": ep.get("leg_id", f"leg-{i}"),
                "city": ep.get("city", city),
                "checkin": ep.get("checkin", "2026-09-12"),
                "checkout": ep.get("checkout", "2026-09-15"),
            })

    return {
        "idempotency_key": "trip-testdigest",
        "digest": "testdigest",
        "status": status,
        "user_id": "demo-lena",
        "envelope": {
            "idempotency_key": "trip-testdigest",
            "legs": legs,
            "day_plans": plans,
        },
    }


# ─── (a) booked TW trip + stub → high tropical_cyclone alert ────────────────

def test_tw_stub_high_alert():
    row = _make_trip_row(iso2="TW", city="Kaohsiung", with_fair_weather=False)
    result = monitor_booked_trip(row, emergency_client=stub_emergency_client)
    assert result["status"] == "ok"
    assert len(result["alerts"]) == 1
    alert = result["alerts"][0]
    assert alert["severity_tier"] == "high"
    assert alert["risk_type"] == "tropical_cyclone"
    assert alert["city"] == "Kaohsiung"
    assert alert["iso2"] == "TW"
    # Fields must be verbatim from the stub (not fabricated)
    assert "Typhoon Podul" in alert["summary"]
    assert alert["source"] == "demo:stub"
    assert alert.get("advice"), "advice must be verbatim from feed, not empty"


# ─── (b) Day carries fair_weather_attractions → switch_wet_weather_variant ──

def test_fair_weather_variant_action():
    row = _make_trip_row(iso2="TW", city="Kaohsiung", with_fair_weather=True)
    result = monitor_booked_trip(row, emergency_client=stub_emergency_client)
    assert result["status"] == "ok"
    assert len(result["alerts"]) == 1
    alert = result["alerts"][0]
    assert alert["suggested_action"] == "switch_wet_weather_variant"
    assert "action_target" in alert
    at = alert["action_target"]
    assert at["kind"] == "replan_switch_variant"
    op = at["replan_op"]
    assert op["op"] == "switch_variant"
    assert op["variant"] == "fair"
    assert isinstance(op["leg_index"], int)
    assert isinstance(op["day_index"], int)


# ─── (c) No variant → reconsider_leg ────────────────────────────────────────

def test_no_variant_reconsider_leg():
    row = _make_trip_row(iso2="TW", city="Kaohsiung", with_fair_weather=False)
    result = monitor_booked_trip(row, emergency_client=stub_emergency_client)
    alert = result["alerts"][0]
    assert alert["suggested_action"] == "reconsider_leg"
    assert "action_target" in alert
    assert alert["action_target"]["kind"] == "webpage_review"


# ─── (d) JP stub monitoring tier ────────────────────────────────────────────

def test_jp_stub_monitoring():
    row = _make_trip_row(iso2="JP", city="Tokyo", with_fair_weather=False)
    result = monitor_booked_trip(row, emergency_client=stub_emergency_client)
    # JP stub returns monitoring (not active) — should surface as monitoring alert
    alerts = result["alerts"]
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["severity_tier"] == "monitoring"
    assert alert["suggested_action"] == "monitor"
    assert "action_target" not in alert, "monitoring tier must NOT have an action_target"


# ─── (e) emergency_client=None → unavailable, no fabricated all-clear ────────

def test_none_client_unavailable():
    row = _make_trip_row(iso2="TW")
    result = monitor_booked_trip(row, emergency_client=None)
    assert result["status"] == "unavailable"
    assert result["alerts"] == []
    assert result["beta_note"], "beta_note must be set when unavailable (honesty)"
    # Absolutely no fabricated alert
    assert len(result["alerts"]) == 0


# ─── (f) Multi-city same-country dedup ───────────────────────────────────────

def test_same_country_dedup():
    """Two TW cities → one alert (dedup by iso2, highest severity)."""
    extra_plan = {
        "leg_id": "leg-1",
        "city": "Taipei",
        "iso2": "TW",
        "checkin": "2026-09-15",
        "checkout": "2026-09-18",
        "days": [{"day_index": 0, "attractions": []}],
    }
    row = _make_trip_row(iso2="TW", city="Kaohsiung", with_fair_weather=False, extra_plans=[extra_plan])
    result = monitor_booked_trip(row, emergency_client=stub_emergency_client)
    assert result["status"] == "ok"
    # Two TW legs queried, but dedup → ONE alert per iso2
    iso2s = [a["iso2"] for a in result["alerts"]]
    assert iso2s.count("TW") == 1, f"Expected one TW alert, got {iso2s}"


# ─── (g) Missing iso2 → beta True in beta_legs ──────────────────────────────

def test_missing_iso2_beta():
    """A leg with no iso2 → marked as beta."""
    row = _make_trip_row(iso2="", city="Unknown City", with_fair_weather=False)
    result = monitor_booked_trip(row, emergency_client=stub_emergency_client)
    assert "leg-0" in result["beta_legs"]
    assert result.get("beta_note"), "beta_note must be set for thin coverage"


# ─── run_aftercare_check: unknown_trip + not_booked ─────────────────────────

class _InMemStore:
    def __init__(self):
        self._trips = {}
        self._users = {}

    def get_trip(self, idk):
        return self._trips.get(idk)

    def get_user(self, uid):
        return self._users.get(uid)

    def save_trip(self, idk, row):
        self._trips[idk] = row

    def save_user(self, uid, row):
        self._users[uid] = row


def test_run_unknown_trip():
    store = _InMemStore()
    result = run_aftercare_check(
        "trip-nonexistent", "",
        store=store, emergency_client=stub_emergency_client
    )
    assert result["outcome"] == "unknown_trip"
    assert result["alerts"] == []


def test_run_not_booked():
    store = _InMemStore()
    row = _make_trip_row(iso2="TW", status="plan_ready")
    store.save_trip("trip-testdigest", row)
    result = run_aftercare_check(
        "trip-testdigest", "",
        store=store, emergency_client=stub_emergency_client
    )
    assert result["outcome"] == "not_booked"
    assert result["alerts"] == []


def test_run_booked_tw_ok():
    store = _InMemStore()
    row = _make_trip_row(iso2="TW", status="booked", with_fair_weather=True)
    store.save_trip("trip-testdigest", row)
    result = run_aftercare_check(
        "trip-testdigest", "",
        store=store, emergency_client=stub_emergency_client,
        sender=None,
    )
    assert result["outcome"] == "ok"
    assert len(result["alerts"]) >= 1
    assert result["alerts"][0]["severity_tier"] == "high"


def test_run_english_alert_not_marked_translated():
    """§3 honesty: an English-language user's alert is the English ORIGINAL — it must
    NOT be flagged translated:true (no auto-translation happened)."""
    store = _InMemStore()
    row = _make_trip_row(iso2="TW", status="booked", with_fair_weather=True)
    store.save_trip("trip-testdigest", row)  # no user in store → lang resolves to 'en'
    result = run_aftercare_check(
        "trip-testdigest", "",
        store=store, emergency_client=stub_emergency_client, sender=None,
    )
    a = result["alerts"][0]
    assert a["lang"] == "en"
    assert a["translated"] is False, "English original must not be labeled auto-translated"
    assert a["summary_localized"] == a["summary"]


# ─── Telegram chat_id resolution: per-user scoping (privacy/correctness) ────
#
# Regression coverage for the fixed bug: an unlinked user's alert used to silently
# fall back to the FIRST TELEGRAM_ALLOWED_USERS entry — an unrelated real user's
# chat_id. That fallback is now removed; the only escape hatch is a double-gated
# demo/test convenience keyed to one explicit, pre-seeded demo user_id.

def _stub_sender_factory():
    """Stub Telegram sender that records every chat_id it is called with."""
    calls: list = []

    def _sender(chat_id, text, *, token="", allowed_users="", **kwargs):
        calls.append(chat_id)
        return {"sent": True, "channel": "telegram", "note": "stub"}

    _sender.calls = calls
    return _sender


def test_telegram_sends_to_users_own_linked_chat_id(monkeypatch):
    """A user WITH their own linked telegram_chat_id -> the alert goes to THEIR
    chat_id specifically, never to the first TELEGRAM_ALLOWED_USERS entry (even
    though an allowed_users list is also configured)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "999999,888888,777777")
    monkeypatch.delenv("TELEGRAM_DEMO_FALLBACK_USER_ID", raising=False)

    store = _InMemStore()
    row = _make_trip_row(iso2="TW", status="booked", with_fair_weather=True)
    row["user_id"] = "demo-lena"
    store.save_trip("trip-testdigest", row)
    store.save_user("demo-lena", {"user_id": "demo-lena", "prefs": {"telegram_chat_id": "111222"}})

    sender = _stub_sender_factory()
    result = run_aftercare_check(
        "trip-testdigest", "demo-lena",
        store=store, emergency_client=stub_emergency_client, sender=sender,
    )
    assert result["telegram"]["attempted"] is True
    assert sender.calls == ["111222"], (
        f"Expected the send to go to the user's own linked chat_id, got {sender.calls}"
    )
    # None of the unrelated TELEGRAM_ALLOWED_USERS entries were ever used.
    for other in ("999999", "888888", "777777"):
        assert other not in sender.calls


def test_telegram_no_leak_to_other_users_allowed_list_entry(monkeypatch):
    """CORE REGRESSION: a user with NO linked telegram_chat_id, not matching any
    demo-fallback condition, must get the honest 'no chat_id resolved' outcome —
    and must NEVER be silently routed to the first (or any) TELEGRAM_ALLOWED_USERS
    entry, which could belong to a completely unrelated real user's chat."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "999999,888888,777777")
    monkeypatch.delenv("TELEGRAM_DEMO_FALLBACK_USER_ID", raising=False)

    store = _InMemStore()
    row = _make_trip_row(iso2="TW", status="booked", with_fair_weather=True)
    row["user_id"] = "demo-lena"
    store.save_trip("trip-testdigest", row)
    # demo-lena exists but has NOT linked a telegram_chat_id.
    store.save_user("demo-lena", {"user_id": "demo-lena", "prefs": {}})

    sender = _stub_sender_factory()
    result = run_aftercare_check(
        "trip-testdigest", "demo-lena",
        store=store, emergency_client=stub_emergency_client, sender=sender,
    )
    assert result["telegram"] == {
        "attempted": False, "sent": False, "note": "no chat_id resolved",
    }
    # The sender must NEVER have been invoked — no fallback to ANY allowed_users entry.
    assert sender.calls == [], (
        f"Expected no send at all, but the sender was called with: {sender.calls}"
    )


def test_telegram_demo_fallback_only_fires_for_designated_demo_user(monkeypatch):
    """The gated demo-fallback (TELEGRAM_DEMO_FALLBACK_USER_ID) must ONLY fire for
    the exact designated demo user_id. A different demo user, also unlinked, must
    still get the honest no-chat_id outcome — never the fallback."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "999999")
    monkeypatch.setenv("TELEGRAM_DEMO_FALLBACK_USER_ID", "demo-lena")

    store = _InMemStore()

    # (i) the designated demo user -> the gated fallback fires.
    row_lena = _make_trip_row(iso2="TW", status="booked", with_fair_weather=True)
    row_lena["user_id"] = "demo-lena"
    store.save_trip("trip-lena", row_lena)
    store.save_user("demo-lena", {"user_id": "demo-lena", "prefs": {}})

    sender_lena = _stub_sender_factory()
    result_lena = run_aftercare_check(
        "trip-lena", "demo-lena",
        store=store, emergency_client=stub_emergency_client, sender=sender_lena,
    )
    assert result_lena["telegram"]["attempted"] is True
    assert sender_lena.calls == ["999999"]

    # (ii) a DIFFERENT demo user, also unlinked, with the SAME env var still set to
    # "demo-lena" -> must NOT receive the fallback (no chat_id resolved instead).
    row_mei = _make_trip_row(iso2="TW", status="booked", with_fair_weather=True)
    row_mei["user_id"] = "demo-mei"
    store.save_trip("trip-mei", row_mei)
    store.save_user("demo-mei", {"user_id": "demo-mei", "prefs": {}})

    sender_mei = _stub_sender_factory()
    result_mei = run_aftercare_check(
        "trip-mei", "demo-mei",
        store=store, emergency_client=stub_emergency_client, sender=sender_mei,
    )
    assert result_mei["telegram"] == {
        "attempted": False, "sent": False, "note": "no chat_id resolved",
    }
    assert sender_mei.calls == []


def test_telegram_demo_fallback_disabled_by_default(monkeypatch):
    """With TELEGRAM_DEMO_FALLBACK_USER_ID unset (the real-deployment default), even
    a genuine pre-seeded demo user with no linked chat_id gets the honest outcome —
    the fallback is opt-in only, never on by default."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "999999")
    monkeypatch.delenv("TELEGRAM_DEMO_FALLBACK_USER_ID", raising=False)

    store = _InMemStore()
    row = _make_trip_row(iso2="TW", status="booked", with_fair_weather=True)
    row["user_id"] = "demo-lena"
    store.save_trip("trip-testdigest", row)
    store.save_user("demo-lena", {"user_id": "demo-lena", "prefs": {}})

    sender = _stub_sender_factory()
    result = run_aftercare_check(
        "trip-testdigest", "demo-lena",
        store=store, emergency_client=stub_emergency_client, sender=sender,
    )
    assert result["telegram"] == {
        "attempted": False, "sent": False, "note": "no chat_id resolved",
    }
    assert sender.calls == []


# ─── Telegram/lang identity resolution: caller-supplied user_id is NOT trusted ──
#
# Regression coverage for the second, unrelated-class bug found in the same
# adversarial pass: `run_aftercare_check`'s /aftercare/check caller supplies an
# UNVERIFIED `user_id` in the request body (the endpoint's own ownership check —
# _authorize_trip_action — is on the TRIP via session_token/owner_token, and its
# docstring says the body's user_id "is no longer an authorization input"). The
# resolution used to prioritize that unverified claim over the trip's actual
# stored owner, so an attacker legitimately authorized for THEIR OWN trip could
# set user_id to a victim's id and have THIS trip's alert (and its Telegram
# delivery) resolved against the victim's linked chat_id/prefs instead of the
# true owner's.

def test_telegram_routes_to_trip_owner_not_caller_claimed_user_id(monkeypatch):
    """CORE REGRESSION: caller claims a DIFFERENT user_id than the trip's actual
    owner. The alert must be resolved (chat_id + language) against the trip's
    real owner ONLY — never against the caller's claim, even though the caller
    was already let past the trip-ownership gate upstream."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "")
    monkeypatch.delenv("TELEGRAM_DEMO_FALLBACK_USER_ID", raising=False)

    store = _InMemStore()
    row = _make_trip_row(iso2="TW", status="booked", with_fair_weather=True)
    row["user_id"] = "demo-lena"  # the trip's TRUE, stored owner
    store.save_trip("trip-testdigest", row)
    store.save_user("demo-lena", {"user_id": "demo-lena", "prefs": {"telegram_chat_id": "111222"}})
    # The "victim": a real, unrelated user with their own linked chat_id.
    store.save_user("victim-mei", {"user_id": "victim-mei", "prefs": {"telegram_chat_id": "999000"}})

    sender = _stub_sender_factory()
    result = run_aftercare_check(
        # Caller CLAIMS to be "victim-mei" in the request body, but is only
        # authorized (upstream, via session/owner token) for demo-lena's trip.
        "trip-testdigest", "victim-mei",
        store=store, emergency_client=stub_emergency_client, sender=sender,
    )
    assert result["telegram"]["attempted"] is True
    assert sender.calls == ["111222"], (
        f"Expected the send to go to the TRIP OWNER's (demo-lena) linked chat_id, "
        f"never the caller-claimed user_id's chat_id. Got {sender.calls}"
    )
    assert "999000" not in sender.calls, (
        "The victim's chat_id must never receive an alert for a trip they don't own, "
        "no matter what user_id the caller claims in the request body."
    )


def test_telegram_no_chat_id_when_caller_claims_id_but_trip_has_no_owner(monkeypatch):
    """A guest trip (no stored owner) must NOT let the caller-supplied user_id
    stand in for a verified owner — honest 'no chat_id resolved' instead of
    routing to whatever user_id the caller happens to claim."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "")
    monkeypatch.delenv("TELEGRAM_DEMO_FALLBACK_USER_ID", raising=False)

    store = _InMemStore()
    row = _make_trip_row(iso2="TW", status="booked", with_fair_weather=True)
    row["user_id"] = ""  # guest trip: no stored owner
    store.save_trip("trip-testdigest", row)
    store.save_user("victim-mei", {"user_id": "victim-mei", "prefs": {"telegram_chat_id": "999000"}})

    sender = _stub_sender_factory()
    result = run_aftercare_check(
        "trip-testdigest", "victim-mei",  # caller claims an unrelated real user's id
        store=store, emergency_client=stub_emergency_client, sender=sender,
    )
    assert result["telegram"] == {
        "attempted": False, "sent": False, "note": "no chat_id resolved",
    }
    assert sender.calls == []


def test_lang_resolved_from_trip_owner_not_caller_claimed_user_id():
    """Language localization must also use the trip's real owner, never the
    caller-supplied user_id — the same identity backs both chat_id and lang."""
    store = _InMemStore()
    row = _make_trip_row(iso2="TW", status="booked", with_fair_weather=True)
    row["user_id"] = "demo-lena"
    store.save_trip("trip-testdigest", row)
    store.save_user("demo-lena", {"user_id": "demo-lena", "prefs": {"lang": "ja"}})
    store.save_user("victim-mei", {"user_id": "victim-mei", "prefs": {"lang": "fr"}})

    result = run_aftercare_check(
        "trip-testdigest", "victim-mei",  # caller claims a different user's id
        store=store, emergency_client=stub_emergency_client, sender=None,
    )
    alerts = result["alerts"]
    assert alerts, "expected at least one alert for this fixture"
    # Should resolve against demo-lena's ("ja") prefs, never victim-mei's ("fr").
    assert alerts[0]["lang"] != "fr"
