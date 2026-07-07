"""test_telegram_readonly.py — READ-ONLY Telegram companion tests.

Covers:
- Itinerary formatting: multi-leg, single-leg, empty
- Day-query parsing: many phrasings, out-of-range, no-trip, unparseable
- Allowlist rejection on inbound
- NO-mutation guarantee: guarded store asserts no write method is ever called
- Token-absent + poll-disabled inertness (run_poll_loop returns immediately)
- Offset advances correctly (no reprocessing of the same update)
"""
from __future__ import annotations

import inspect
import os

import pytest

from utils.telegram_companion import (
    format_day,
    format_itinerary,
    handle_update,
    is_itinerary_query,
    parse_day_query,
    poll_once,
    run_poll_loop,
    send_itinerary,
)


# ─── Shared fixtures / helpers ────────────────────────────────────────────────

def _make_trip(
    city_legs: list[dict] | None = None,
    *,
    status: str = "booked",
    key: str = "trip-test-abc",
) -> dict:
    """Build a minimal trip row with one or more legs and day_plans."""
    if city_legs is None:
        city_legs = [
            {
                "city": "Tokyo",
                "iso2": "JP",
                "checkin": "2026-10-01",
                "checkout": "2026-10-04",
                "days": [
                    {
                        "day_index": 0,
                        "attractions": [
                            {"name": "Senso-ji Temple", "category": "temple"},
                            {"name": "Shinjuku Gyoen", "category": "park"},
                        ],
                        "meals": {
                            "lunch": {"name": "Ichiran Ramen"},
                            "dinner": {"name": "Sushi Saito"},
                        },
                    },
                    {
                        "day_index": 1,
                        "attractions": [{"name": "teamLab Borderless"}],
                        "meals": {},
                    },
                    {
                        "day_index": 2,
                        "attractions": [],
                        "meals": {},
                    },
                ],
            }
        ]

    legs = []
    day_plans = []
    for i, cl in enumerate(city_legs):
        lid = f"leg-{i}"
        legs.append({
            "leg_id": lid,
            "city": cl["city"],
            "iso2": cl.get("iso2", ""),
            "checkin": cl.get("checkin", ""),
            "checkout": cl.get("checkout", ""),
        })
        day_plans.append({
            "leg_id": lid,
            "city": cl["city"],
            "iso2": cl.get("iso2", ""),
            "checkin": cl.get("checkin", ""),
            "checkout": cl.get("checkout", ""),
            "days": cl.get("days", []),
        })

    return {
        "idempotency_key": key,
        "status": status,
        "user_id": "demo-test",
        "envelope": {"legs": legs, "day_plans": day_plans},
    }


def _fake_post_ok(url, *, headers=None, json=None):
    return {"ok": True, "result": {"message_id": 1}}


def _fake_post_err(url, *, headers=None, json=None):
    return {"ok": False, "description": "Bad Request"}


# ─── Itinerary formatting ─────────────────────────────────────────────────────

class TestFormatItinerary:
    def test_multi_leg(self):
        trip = _make_trip([
            {
                "city": "Tokyo",
                "iso2": "JP",
                "checkin": "2026-10-01",
                "checkout": "2026-10-04",
                "days": [
                    {"day_index": 0,
                     "attractions": [{"name": "Senso-ji"}, {"name": "Shibuya"}],
                     "meals": {"lunch": {"name": "Ramen Bar"}}},
                ],
            },
            {
                "city": "Kyoto",
                "iso2": "JP",
                "checkin": "2026-10-04",
                "checkout": "2026-10-07",
                "days": [
                    {"day_index": 0,
                     "attractions": [{"name": "Fushimi Inari"}],
                     "meals": {}},
                ],
            },
        ])
        text = format_itinerary(trip)
        assert "Tokyo" in text
        assert "Kyoto" in text
        assert "Senso-ji" in text
        assert "Fushimi Inari" in text
        assert "2026-10-01" in text
        assert "2026-10-04" in text
        assert "Day 1" in text

    def test_single_leg(self):
        trip = _make_trip()
        text = format_itinerary(trip)
        assert "Tokyo" in text
        assert "Senso-ji Temple" in text
        assert "Ichiran Ramen" in text
        assert "Day 1" in text
        assert "Day 2" in text
        assert "Day 3" in text

    def test_empty_day_plans(self):
        trip = {"idempotency_key": "trip-empty", "status": "booked",
                "envelope": {"legs": [], "day_plans": []}}
        text = format_itinerary(trip)
        assert "No itinerary data" in text

    def test_leg_without_attractions(self):
        trip = _make_trip([{
            "city": "Bali",
            "iso2": "ID",
            "checkin": "2026-11-01",
            "checkout": "2026-11-03",
            "days": [{"day_index": 0, "attractions": [], "meals": {}}],
        }])
        text = format_itinerary(trip)
        assert "Bali" in text
        assert "No attractions scheduled" in text

    def test_html_entities_escaped(self):
        """City names with HTML chars must be escaped."""
        trip = _make_trip([{
            "city": "<script>alert(1)</script>",
            "iso2": "XX",
            "days": [{"day_index": 0, "attractions": [], "meals": {}}],
        }])
        text = format_itinerary(trip)
        assert "<script>" not in text
        assert "&lt;script&gt;" in text

    def test_itinerary_reply_hint_present(self):
        trip = _make_trip()
        text = format_itinerary(trip)
        assert "itinerary" in text.lower()
        assert "day N" in text or "day n" in text.lower()

    def test_no_callback_data_in_output(self):
        """format_itinerary must never embed callback_data (display-only)."""
        trip = _make_trip()
        text = format_itinerary(trip)
        assert "callback_data" not in text


# ─── format_day ───────────────────────────────────────────────────────────────

class TestFormatDay:
    def test_day1(self):
        trip = _make_trip()
        text = format_day(trip, 1)
        assert "Day 1" in text
        assert "Tokyo" in text
        assert "Senso-ji Temple" in text

    def test_day2(self):
        trip = _make_trip()
        text = format_day(trip, 2)
        assert "Day 2" in text
        assert "teamLab" in text

    def test_day3_no_attractions(self):
        trip = _make_trip()
        text = format_day(trip, 3)
        assert "Day 3" in text
        assert "No attractions" in text

    def test_out_of_range_high(self):
        trip = _make_trip()
        text = format_day(trip, 99)
        assert "out of range" in text
        assert "3 day" in text

    def test_out_of_range_zero(self):
        trip = _make_trip()
        text = format_day(trip, 0)
        assert "out of range" in text

    def test_out_of_range_negative(self):
        trip = _make_trip()
        text = format_day(trip, -1)
        assert "out of range" in text

    def test_multi_leg_day_numbering(self):
        """Day numbering is global across legs."""
        trip = _make_trip([
            {
                "city": "Tokyo",
                "iso2": "JP",
                "days": [
                    {"day_index": 0, "attractions": [{"name": "Senso-ji"}], "meals": {}},
                    {"day_index": 1, "attractions": [{"name": "Shibuya"}], "meals": {}},
                ],
            },
            {
                "city": "Osaka",
                "iso2": "JP",
                "days": [
                    {"day_index": 0, "attractions": [{"name": "Dotonbori"}], "meals": {}},
                ],
            },
        ])
        # Day 3 = Osaka day 1
        text = format_day(trip, 3)
        assert "Osaka" in text
        assert "Dotonbori" in text

    def test_empty_trip(self):
        trip = {"idempotency_key": "trip-e", "envelope": {"day_plans": []}}
        text = format_day(trip, 1)
        assert "No itinerary" in text

    def test_meals_shown(self):
        trip = _make_trip()
        text = format_day(trip, 1)
        assert "Lunch" in text or "lunch" in text.lower()
        assert "Ramen" in text


# ─── parse_day_query ──────────────────────────────────────────────────────────

class TestParseDayQuery:
    @pytest.mark.parametrize("text,expected", [
        ("day 3", 3),
        ("day3", 3),
        ("day-3", 3),
        ("/day 3", 3),
        ("/day3", 3),
        ("on day 3", 3),
        ("what to do on day 3", 3),
        ("what to do on day 10", 10),
        ("what should I do on day 5", 5),
        ("Day 1", 1),
        ("DAY 2", 2),
        ("Show me day 7", 7),
    ])
    def test_parses(self, text, expected):
        assert parse_day_query(text) == expected

    @pytest.mark.parametrize("text", [
        "itinerary",
        "plan",
        "schedule",
        "hello",
        "",
        "   ",
        "what is the weather",
        "book a flight",
        # \b word-boundary: the "day" tail of other words must NOT parse as a day query
        "Monday 3pm",
        "holiday 5",
        "my birthday 4",
        "someday 9",
    ])
    def test_not_a_day_query(self, text):
        assert parse_day_query(text) is None


# ─── is_itinerary_query ───────────────────────────────────────────────────────

class TestIsItineraryQuery:
    @pytest.mark.parametrize("text", [
        "itinerary", "my itinerary", "show itinerary",
        "plan", "my plan", "trip", "schedule",
        "Itinerary", "PLAN",
    ])
    def test_true(self, text):
        assert is_itinerary_query(text) is True

    @pytest.mark.parametrize("text", [
        "day 3", "/day 3", "hello", "", "what to eat",
    ])
    def test_false(self, text):
        assert is_itinerary_query(text) is False


# ─── Allowlist rejection on inbound ──────────────────────────────────────────

class TestAllowlistRejection:
    def _make_update(self, chat_id: str, text: str) -> dict:
        return {
            "update_id": 1,
            "message": {
                "chat": {"id": chat_id},
                "text": text,
            },
        }

    def _make_store(self):
        class _NoOpStore:
            def list_demo_users(self):
                return []

            def get_user(self, uid):
                return None

            def list_trips(self, uid):
                return []

            def get_trip(self, key):
                return None

        return _NoOpStore()

    def test_non_allowlisted_ignored(self):
        update = self._make_update("99999", "itinerary")
        result = handle_update(
            update,
            store=self._make_store(),
            token="FAKE-TOKEN",
            allowed_users="11111,22222",
            _post=_fake_post_ok,
        )
        assert result is None, "Non-allowlisted chat_id must be silently ignored"

    def test_allowlisted_proceeds(self):
        update = self._make_update("11111", "itinerary")
        http_calls = []

        def capture_post(url, *, headers=None, json=None):
            http_calls.append(json)
            return {"ok": True}

        result = handle_update(
            update,
            store=self._make_store(),
            token="FAKE-TOKEN",
            allowed_users="11111,22222",
            _post=capture_post,
        )
        # Result is a send-result (sent or not); no crash and not None
        assert result is not None

    def test_empty_allowlist_allows_all(self):
        """Empty allowed_users → no restriction."""
        update = self._make_update("99999", "itinerary")
        result = handle_update(
            update,
            store=self._make_store(),
            token="FAKE-TOKEN",
            allowed_users="",
            _post=_fake_post_ok,
        )
        # Not silently ignored
        assert result is not None


# ─── NO-mutation guarantee ────────────────────────────────────────────────────

class _GuardedStore:
    """Store that raises AssertionError on any write / transactional call."""

    def __init__(self, users=None, trips=None):
        self._users: dict = users or {}
        self._trips: dict = trips or {}

    def list_demo_users(self):
        return list(self._users.values())

    def get_user(self, uid: str):
        return self._users.get(uid)

    def list_trips(self, uid: str):
        return [
            {"idempotency_key": k, "status": v["status"]}
            for k, v in self._trips.items()
            if v.get("user_id") == uid
        ]

    def get_trip(self, key: str):
        return self._trips.get(key)

    # ─── FORBIDDEN transactional methods — must never be called ──────────────
    def save_plan(self, *a, **kw):
        raise AssertionError("telegram_companion called save_plan — MUTATION VIOLATION")

    def mark_booked(self, *a, **kw):
        raise AssertionError("telegram_companion called mark_booked — MUTATION VIOLATION")

    def mark_cancelled(self, *a, **kw):
        raise AssertionError("telegram_companion called mark_cancelled — MUTATION VIOLATION")

    def update_envelope(self, *a, **kw):
        raise AssertionError("telegram_companion called update_envelope — MUTATION VIOLATION")

    def complete_checkout(self, *a, **kw):
        raise AssertionError("telegram_companion called complete_checkout — MUTATION VIOLATION")


def _make_guarded_store_with_trip() -> tuple[_GuardedStore, str]:
    chat_id = "12345"
    user = {
        "user_id": "demo-guard-test",
        "display_name": "Guard Tester",
        "prefs": {"telegram_chat_id": chat_id},
    }
    trip_key = "trip-guard-test"
    trip = {
        "idempotency_key": trip_key,
        "status": "booked",
        "user_id": "demo-guard-test",
        "envelope": {
            "legs": [{"leg_id": "leg-0", "city": "Seoul", "iso2": "KR",
                      "checkin": "2026-12-01", "checkout": "2026-12-03"}],
            "day_plans": [{
                "leg_id": "leg-0",
                "city": "Seoul",
                "iso2": "KR",
                "days": [
                    {"day_index": 0,
                     "attractions": [{"name": "Gyeongbokgung Palace"}],
                     "meals": {}},
                    {"day_index": 1,
                     "attractions": [{"name": "Namsan Tower"}],
                     "meals": {}},
                ],
            }],
        },
    }
    store = _GuardedStore(
        users={"demo-guard-test": user},
        trips={trip_key: trip},
    )
    return store, chat_id


class TestNoMutation:
    def _make_update(self, chat_id: str, text: str, update_id: int = 1) -> dict:
        return {
            "update_id": update_id,
            "message": {"chat": {"id": chat_id}, "text": text},
        }

    def test_itinerary_query_no_mutation(self):
        store, chat_id = _make_guarded_store_with_trip()
        # Must not raise AssertionError from guarded methods
        result = handle_update(
            self._make_update(chat_id, "itinerary"),
            store=store,
            token="FAKE-TOKEN",
            allowed_users="",
            _post=_fake_post_ok,
        )
        assert result is not None

    def test_day_query_no_mutation(self):
        store, chat_id = _make_guarded_store_with_trip()
        result = handle_update(
            self._make_update(chat_id, "day 1"),
            store=store,
            token="FAKE-TOKEN",
            allowed_users="",
            _post=_fake_post_ok,
        )
        assert result is not None

    def test_unknown_query_no_mutation(self):
        store, chat_id = _make_guarded_store_with_trip()
        result = handle_update(
            self._make_update(chat_id, "hello there"),
            store=store,
            token="FAKE-TOKEN",
            allowed_users="",
            _post=_fake_post_ok,
        )
        assert result is not None

    def test_poll_once_no_mutation(self):
        store, chat_id = _make_guarded_store_with_trip()

        def fake_get_updates(url, *, params=None):
            return {
                "ok": True,
                "result": [
                    {"update_id": 42, "message": {"chat": {"id": chat_id}, "text": "itinerary"}},
                    {"update_id": 43, "message": {"chat": {"id": chat_id}, "text": "day 2"}},
                ],
            }

        results, new_offset = poll_once(
            token="FAKE-TOKEN",
            allowed_users="",
            offset=0,
            store=store,
            _get_updates=fake_get_updates,
            _post=_fake_post_ok,
        )
        assert new_offset == 44  # max(42, 43) + 1
        # No mutation errors raised means the guarded store was never called for writes

    def test_send_itinerary_no_mutation(self):
        store, chat_id = _make_guarded_store_with_trip()
        trip_key = "trip-guard-test"
        trip_row = store.get_trip(trip_key)
        # Must not raise
        result = send_itinerary(
            trip_row,
            chat_id=chat_id,
            token="FAKE-TOKEN",
            allowed_users="",
            _post=_fake_post_ok,
        )
        assert result is not None


class TestStaticNoMutationGuard:
    """Static source check: telegram_companion.py must never reference forbidden symbols."""

    FORBIDDEN = [
        "mark_booked",
        "mark_cancelled",
        "save_plan",
        "update_envelope",
        "complete_checkout",
        "_wallet_debit",
        "cancel_checkout",
    ]

    def _get_source(self) -> str:
        import importlib
        mod = importlib.import_module("utils.telegram_companion")
        return inspect.getsource(mod)

    def _has_call_site(self, src: str, sym: str) -> list[str]:
        """Find actual call-site references (not just comments/docstrings)."""
        call_patterns = [f"{sym}(", f".{sym}(", f"store.{sym}"]
        return [
            line for line in src.splitlines()
            if not line.strip().startswith("#")
            and '"""' not in line.strip()[:3]
            and "'''" not in line.strip()[:3]
            and any(p in line for p in call_patterns)
        ]

    def test_no_forbidden_symbols(self):
        src = self._get_source()
        for sym in self.FORBIDDEN:
            hits = self._has_call_site(src, sym)
            assert not hits, (
                f"Forbidden call-site '{sym}' found in telegram_companion.py: {hits}"
            )


# ─── Token-absent + poll-disabled inertness ───────────────────────────────────

class TestInertness:
    def test_no_token_run_poll_loop_inert(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_POLL_ENABLED", raising=False)
        network_calls = []

        def bad_get_updates(*a, **kw):
            network_calls.append(True)
            return {"ok": True, "result": []}

        class _EmptyStore:
            def list_demo_users(self): return []
            def get_user(self, u): return None
            def list_trips(self, u): return []
            def get_trip(self, k): return None

        run_poll_loop(store=_EmptyStore(), _get_updates=bad_get_updates)
        assert len(network_calls) == 0, (
            "run_poll_loop must not make network calls when TELEGRAM_BOT_TOKEN is absent"
        )

    def test_token_set_but_poll_disabled(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE-TOKEN")
        monkeypatch.delenv("TELEGRAM_POLL_ENABLED", raising=False)
        network_calls = []

        def bad_get_updates(*a, **kw):
            network_calls.append(True)
            return {"ok": True, "result": []}

        class _EmptyStore:
            def list_demo_users(self): return []
            def get_user(self, u): return None
            def list_trips(self, u): return []
            def get_trip(self, k): return None

        run_poll_loop(store=_EmptyStore(), _get_updates=bad_get_updates)
        assert len(network_calls) == 0, (
            "run_poll_loop must not make network calls when TELEGRAM_POLL_ENABLED is absent"
        )

    def test_poll_enabled_false_string_inert(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE-TOKEN")
        monkeypatch.setenv("TELEGRAM_POLL_ENABLED", "false")
        network_calls = []

        def bad_get_updates(*a, **kw):
            network_calls.append(True)
            return {"ok": True, "result": []}

        class _EmptyStore:
            def list_demo_users(self): return []
            def get_user(self, u): return None
            def list_trips(self, u): return []
            def get_trip(self, k): return None

        run_poll_loop(store=_EmptyStore(), _get_updates=bad_get_updates)
        assert len(network_calls) == 0

    def test_no_import_time_network(self):
        """Importing telegram_companion must not trigger any network I/O."""
        import importlib
        import sys
        # Remove cached module if any
        for key in list(sys.modules.keys()):
            if "telegram_companion" in key:
                del sys.modules[key]
        # This must not raise or do network I/O (no network mock needed — just import)
        mod = importlib.import_module("utils.telegram_companion")
        assert mod is not None


# ─── Offset advances / no reprocessing ───────────────────────────────────────

class TestOffsetAdvances:
    def _empty_store(self):
        class _S:
            def list_demo_users(self): return []
            def get_user(self, u): return None
            def list_trips(self, u): return []
            def get_trip(self, k): return None
        return _S()

    def test_offset_advances_past_max_update_id(self):
        calls = []

        def fake_get_updates(url, *, params=None):
            calls.append(params.get("offset"))
            return {
                "ok": True,
                "result": [
                    {"update_id": 100, "message": {"chat": {"id": "1"}, "text": "day 1"}},
                    {"update_id": 102, "message": {"chat": {"id": "1"}, "text": "day 2"}},
                    {"update_id": 101, "message": {"chat": {"id": "1"}, "text": "itinerary"}},
                ],
            }

        _, new_offset = poll_once(
            token="FAKE-TOKEN",
            offset=0,
            store=self._empty_store(),
            _get_updates=fake_get_updates,
            _post=_fake_post_ok,
        )
        assert new_offset == 103, f"Expected 103, got {new_offset}"

    def test_offset_passed_to_next_call(self):
        """On the second poll_once call, offset from the previous call is used."""
        first_call_offset = []
        second_call_offset = []
        call_count = [0]

        def fake_get_updates(url, *, params=None):
            call_count[0] += 1
            if call_count[0] == 1:
                first_call_offset.append(params.get("offset"))
                return {
                    "ok": True,
                    "result": [
                        {"update_id": 50, "message": {"chat": {"id": "1"}, "text": "day 1"}},
                    ],
                }
            else:
                second_call_offset.append(params.get("offset"))
                return {"ok": True, "result": []}

        _, offset1 = poll_once(
            token="FAKE-TOKEN", offset=0, store=self._empty_store(),
            _get_updates=fake_get_updates, _post=_fake_post_ok,
        )
        assert offset1 == 51

        _, offset2 = poll_once(
            token="FAKE-TOKEN", offset=offset1, store=self._empty_store(),
            _get_updates=fake_get_updates, _post=_fake_post_ok,
        )
        assert second_call_offset[0] == 51, (
            "Second poll_once must pass offset=51 to getUpdates"
        )

    def test_empty_result_offset_unchanged(self):
        def fake_get_updates(url, *, params=None):
            return {"ok": True, "result": []}

        _, new_offset = poll_once(
            token="FAKE-TOKEN", offset=77, store=self._empty_store(),
            _get_updates=fake_get_updates, _post=_fake_post_ok,
        )
        assert new_offset == 77, "Offset must not change on empty result"

    def test_getUpdates_error_offset_unchanged(self):
        def bad_get_updates(url, *, params=None):
            raise RuntimeError("network error")

        _, new_offset = poll_once(
            token="FAKE-TOKEN", offset=55, store=self._empty_store(),
            _get_updates=bad_get_updates, _post=_fake_post_ok,
        )
        assert new_offset == 55

    def test_getUpdates_not_ok_offset_unchanged(self):
        def fake_get_updates(url, *, params=None):
            return {"ok": False, "description": "Unauthorized"}

        _, new_offset = poll_once(
            token="FAKE-TOKEN", offset=33, store=self._empty_store(),
            _get_updates=fake_get_updates, _post=_fake_post_ok,
        )
        assert new_offset == 33


# ─── handle_update: no-trip / out-of-range honest replies ────────────────────

class TestHonestReplies:
    def _no_trip_store(self):
        class _S:
            def list_demo_users(self): return []
            def get_user(self, u): return None
            def list_trips(self, u): return []
            def get_trip(self, k): return None
        return _S()

    def _make_update(self, text: str, chat_id: str = "11111") -> dict:
        return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}

    def test_no_trip_day_query(self):
        captured = {}

        def capture_post(url, *, headers=None, json=None):
            captured["payload"] = json
            return {"ok": True}

        handle_update(
            self._make_update("day 3"),
            store=self._no_trip_store(),
            token="FAKE-TOKEN",
            allowed_users="",
            _post=capture_post,
        )
        sent_text = captured["payload"]["text"]
        assert "No booked trip" in sent_text or "no booked trip" in sent_text.lower()

    def test_no_trip_itinerary_query(self):
        captured = {}

        def capture_post(url, *, headers=None, json=None):
            captured["payload"] = json
            return {"ok": True}

        handle_update(
            self._make_update("itinerary"),
            store=self._no_trip_store(),
            token="FAKE-TOKEN",
            allowed_users="",
            _post=capture_post,
        )
        sent_text = captured["payload"]["text"]
        assert "No booked trip" in sent_text or "no booked trip" in sent_text.lower()

    def test_unknown_query_help_reply(self):
        captured = {}

        def capture_post(url, *, headers=None, json=None):
            captured["payload"] = json
            return {"ok": True}

        handle_update(
            self._make_update("book me a flight to Paris"),
            store=self._no_trip_store(),
            token="FAKE-TOKEN",
            allowed_users="",
            _post=capture_post,
        )
        sent_text = captured["payload"]["text"]
        # Must include the read-only reminder
        assert "webpage" in sent_text.lower() or "read-only" in sent_text.lower() or \
               "Travel Guild" in sent_text

    def test_out_of_range_day_reply(self):
        store, chat_id = _make_guarded_store_with_trip()
        captured = {}

        def capture_post(url, *, headers=None, json=None):
            captured["payload"] = json
            return {"ok": True}

        handle_update(
            self._make_update("day 999", chat_id),
            store=store,
            token="FAKE-TOKEN",
            allowed_users="",
            _post=capture_post,
        )
        sent_text = captured["payload"]["text"]
        assert "out of range" in sent_text.lower() or "999" in sent_text

    def test_no_callback_data_in_any_reply(self):
        """No reply must ever contain callback_data."""
        import json as json_mod
        store, chat_id = _make_guarded_store_with_trip()
        payloads = []

        def capture_post(url, *, headers=None, json=None):
            payloads.append(json_mod.dumps(json or {}))
            return {"ok": True}

        for text in ("itinerary", "day 1", "day 99", "hello"):
            update = self._make_update(text, chat_id)
            handle_update(
                update, store=store, token="FAKE-TOKEN",
                allowed_users="", _post=capture_post,
            )

        for p in payloads:
            assert "callback_data" not in p, f"callback_data found in reply: {p}"


# ─── send_itinerary helper ────────────────────────────────────────────────────

class TestSendItinerary:
    def test_sends_formatted_itinerary(self):
        trip = _make_trip()
        captured = {}

        def capture_post(url, *, headers=None, json=None):
            captured["payload"] = json
            return {"ok": True}

        result = send_itinerary(
            trip,
            chat_id="11111",
            token="FAKE-TOKEN",
            allowed_users="11111",
            _post=capture_post,
        )
        assert result["sent"] is True
        assert "Tokyo" in captured["payload"]["text"]

    def test_no_token_disabled(self):
        trip = _make_trip()
        result = send_itinerary(
            trip,
            chat_id="11111",
            token="",
            _post=_fake_post_ok,
        )
        assert result["sent"] is False
        assert "disabled" in result["note"].lower() or "token" in result["note"].lower()

    def test_not_in_allowlist(self):
        trip = _make_trip()
        result = send_itinerary(
            trip,
            chat_id="99999",
            token="FAKE-TOKEN",
            allowed_users="11111,22222",
            _post=_fake_post_ok,
        )
        assert result["sent"] is False
        assert "allowlist" in result["note"].lower()


# ─── resolve_chat_id_to_trip — store error fallbacks ────────────────────────

class TestResolveChatIdStoreErrors:
    """resolve_chat_id_to_trip must degrade to None on any store error (never propagate)."""

    def _make_store(self, **overrides):
        """Return a MagicMock store; override specific methods via keyword args."""
        import unittest.mock as mock
        store = mock.MagicMock()
        store.list_demo_users.return_value = [{"user_id": "u1"}]
        store.get_user.return_value = {
            "prefs": {"telegram_chat_id": "42"}
        }
        store.list_trips.return_value = [{"status": "booked", "idempotency_key": "trip-abc"}]
        store.get_trip.return_value = {"idempotency_key": "trip-abc", "status": "booked"}
        for method, side_effect in overrides.items():
            getattr(store, method).side_effect = side_effect
        return store

    def _resolve(self, store, chat_id="42"):
        import sys, os
        _soc = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _soc not in sys.path:
            sys.path.insert(0, _soc)
        from utils.telegram_companion import resolve_chat_id_to_trip
        return resolve_chat_id_to_trip(chat_id, store=store)

    def test_list_demo_users_raises_returns_none(self):
        store = self._make_store(list_demo_users=RuntimeError("db down"))
        assert self._resolve(store) is None

    def test_get_user_raises_continues_to_next_user_returns_none(self):
        """get_user raising must be swallowed (continue), not propagated."""
        store = self._make_store(get_user=RuntimeError("user db fail"))
        assert self._resolve(store) is None

    def test_get_user_returns_non_dict_skipped_returns_none(self):
        """A non-dict user row (e.g. None or a string) is silently skipped."""
        import unittest.mock as mock
        store = self._make_store()
        store.get_user.side_effect = None
        store.get_user.return_value = "not-a-dict"
        assert self._resolve(store) is None

    def test_list_trips_raises_returns_none(self):
        store = self._make_store(list_trips=RuntimeError("trips db fail"))
        assert self._resolve(store) is None

    def test_get_trip_raises_continues_returns_none(self):
        store = self._make_store(get_trip=RuntimeError("trip fetch fail"))
        assert self._resolve(store) is None

    def test_booked_trip_with_empty_idempotency_key_skipped(self):
        """A booked trip with empty idempotency_key must be skipped (not fetched)."""
        import unittest.mock as mock
        store = self._make_store()
        store.list_trips.side_effect = None
        store.list_trips.return_value = [{"status": "booked", "idempotency_key": ""}]
        result = self._resolve(store)
        assert result is None
        store.get_trip.assert_not_called()

    def test_happy_path_returns_trip(self):
        """Baseline: with a matching chat_id and booked trip, returns the trip dict."""
        store = self._make_store()
        result = self._resolve(store, chat_id="42")
        assert result is not None
        assert result.get("idempotency_key") == "trip-abc"


# ─── /start <token> account-linking handler ───────────────────────────────────

class _LinkableStore:
    """Minimal store supporting get_user/upsert_demo_user for the linking flow."""

    def __init__(self, users: dict[str, dict]):
        self._users = users

    def list_demo_users(self):
        return [{"user_id": k} for k in self._users]

    def get_user(self, uid: str):
        return self._users.get(uid)

    def upsert_demo_user(self, row: dict):
        uid = row["user_id"]
        existing = self._users.get(uid, {})
        merged = {**existing, **row}
        self._users[uid] = merged

    def list_trips(self, uid: str):
        return []

    def get_trip(self, key: str):
        return None


def _make_start_update(chat_id: str, link_token: str, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": f"/start {link_token}"},
    }


class TestStartLinkHandler:
    def test_valid_token_persists_chat_id_and_confirms(self, monkeypatch):
        import sys, os
        _soc = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _soc not in sys.path:
            sys.path.insert(0, _soc)
        from utils.telegram_link import generate_link_token

        store = _LinkableStore({
            "demo-mei": {"user_id": "demo-mei", "display_name": "Mei",
                         "prefs": {}},
        })
        token = generate_link_token("demo-mei")

        captured = {}

        def capture_post(url, *, headers=None, json=None):
            captured["payload"] = json
            return {"ok": True}

        result = handle_update(
            _make_start_update("999888", token),
            store=store,
            token="FAKE-TOKEN",
            allowed_users="",
            _post=capture_post,
        )
        assert result is not None
        sent_text = captured["payload"]["text"]
        assert "Connected" in sent_text or "connected" in sent_text.lower()

        # chat_id must now be persisted on the resolved user.
        assert store.get_user("demo-mei")["prefs"]["telegram_chat_id"] == "999888"

    def test_invalid_token_honest_reply_no_crash(self):
        store = _LinkableStore({})
        captured = {}

        def capture_post(url, *, headers=None, json=None):
            captured["payload"] = json
            return {"ok": True}

        result = handle_update(
            _make_start_update("111222", "not-a-real-token"),
            store=store,
            token="FAKE-TOKEN",
            allowed_users="",
            _post=capture_post,
        )
        assert result is not None
        sent_text = captured["payload"]["text"]
        assert "expired" in sent_text.lower() or "invalid" in sent_text.lower()

    def test_token_valid_but_user_gone_honest_reply_no_crash(self):
        import sys, os
        _soc = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _soc not in sys.path:
            sys.path.insert(0, _soc)
        from utils.telegram_link import generate_link_token

        # Token resolves to a user_id, but the store has no such user (deleted
        # between issue and resolve — unlikely for demo users, but must not crash).
        store = _LinkableStore({})
        token = generate_link_token("demo-ghost")

        captured = {}

        def capture_post(url, *, headers=None, json=None):
            captured["payload"] = json
            return {"ok": True}

        result = handle_update(
            _make_start_update("333444", token),
            store=store,
            token="FAKE-TOKEN",
            allowed_users="",
            _post=capture_post,
        )
        assert result is not None
        sent_text = captured["payload"]["text"]
        assert "could not be found" in sent_text.lower() or "no longer exists" in sent_text.lower()

    def test_token_is_single_use_via_bot_flow(self):
        """A second /start with the SAME token (e.g. a re-sent message) must not
        re-resolve — the second attempt gets the honest expired/invalid reply."""
        import sys, os
        _soc = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _soc not in sys.path:
            sys.path.insert(0, _soc)
        from utils.telegram_link import generate_link_token

        store = _LinkableStore({
            "demo-alex": {"user_id": "demo-alex", "display_name": "Alex", "prefs": {}},
        })
        token = generate_link_token("demo-alex")

        payloads = []

        def capture_post(url, *, headers=None, json=None):
            payloads.append(json)
            return {"ok": True}

        handle_update(_make_start_update("555", token, update_id=1),
                      store=store, token="FAKE-TOKEN", allowed_users="", _post=capture_post)
        handle_update(_make_start_update("555", token, update_id=2),
                      store=store, token="FAKE-TOKEN", allowed_users="", _post=capture_post)

        assert "Connected" in payloads[0]["text"] or "connected" in payloads[0]["text"].lower()
        assert "expired" in payloads[1]["text"].lower() or "invalid" in payloads[1]["text"].lower()

    def test_start_dispatch_does_not_mutate_forbidden_store_methods(self):
        """The /start branch must still respect the trip-mutation boundary — it
        may call upsert_demo_user (account linking) but never any of the
        forbidden trip-transactional methods."""
        import sys, os
        _soc = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _soc not in sys.path:
            sys.path.insert(0, _soc)
        from utils.telegram_link import generate_link_token

        class _GuardedLinkableStore(_LinkableStore):
            def save_plan(self, *a, **kw):
                raise AssertionError("MUTATION VIOLATION: save_plan")
            def mark_booked(self, *a, **kw):
                raise AssertionError("MUTATION VIOLATION: mark_booked")
            def mark_cancelled(self, *a, **kw):
                raise AssertionError("MUTATION VIOLATION: mark_cancelled")
            def update_envelope(self, *a, **kw):
                raise AssertionError("MUTATION VIOLATION: update_envelope")
            def complete_checkout(self, *a, **kw):
                raise AssertionError("MUTATION VIOLATION: complete_checkout")

        store = _GuardedLinkableStore({
            "demo-lena": {"user_id": "demo-lena", "display_name": "Lena", "prefs": {}},
        })
        token = generate_link_token("demo-lena")

        result = handle_update(
            _make_start_update("777", token),
            store=store, token="FAKE-TOKEN", allowed_users="",
            _post=lambda url, **kw: {"ok": True},
        )
        assert result is not None
