"""test_aftercare_lang.py — #100 AFTERCARE: aftercare_lang resolve_lang + translate_alert tests.

Tests:
- resolve_lang precedence: prefs.lang > nationality > 'en'
- translate_alert with injected _post → returns translation
- translate_alert no key → None, caller sets translated:false + keeps English summary
- en path makes no LLM call (summary_localized == summary)
"""
from __future__ import annotations

import pytest

from utils.aftercare_lang import resolve_lang, translate_alert, NATIONALITY_LANG


# ─── resolve_lang ────────────────────────────────────────────────────────────

def test_resolve_lang_prefs_override():
    """prefs.lang takes priority over nationality."""
    user = {"nationality": "DE", "prefs": {"lang": "fr"}}
    assert resolve_lang(user) == "fr"


def test_resolve_lang_nationality():
    """Falls back to nationality mapping when no prefs.lang."""
    user = {"nationality": "DE", "prefs": {}}
    assert resolve_lang(user) == "de"


def test_resolve_lang_jp():
    user = {"nationality": "JP", "prefs": {}}
    assert resolve_lang(user) == "ja"


def test_resolve_lang_unknown_nationality():
    """Unknown nationality → 'en'."""
    user = {"nationality": "ZZ", "prefs": {}}
    assert resolve_lang(user) == "en"


def test_resolve_lang_sg_is_english():
    """SG (Singapore) → 'en' (not in NATIONALITY_LANG map)."""
    user = {"nationality": "SG", "prefs": {}}
    assert resolve_lang(user) == "en"


def test_resolve_lang_none_user():
    """None user → 'en'."""
    assert resolve_lang(None) == "en"


def test_resolve_lang_no_prefs():
    """User with no prefs key at all."""
    user = {"nationality": "DE"}
    assert resolve_lang(user) == "de"


# ─── translate_alert ─────────────────────────────────────────────────────────

def _mock_post_ok(url, *, headers, json):
    """Simulates a successful DASHSCOPE response."""
    return {
        "choices": [{"message": {"content": "Taifun Podul — Landung über Südtaiwan."}}]
    }


def _mock_post_empty(url, *, headers, json):
    """Simulates an empty DASHSCOPE response."""
    return {"choices": [{"message": {"content": ""}}]}


def _mock_post_error(url, *, headers, json):
    """Simulates a DASHSCOPE error."""
    raise RuntimeError("network error")


def test_translate_alert_happy_path():
    """Injected _post returns a translation → returns the translation string."""
    result = translate_alert("Typhoon Podul — landfall over southern Taiwan.", "de", _post=_mock_post_ok)
    assert result is not None
    assert "Taifun" in result


def test_translate_alert_empty_response_returns_none():
    """Empty content from LLM → None (honest fallback)."""
    result = translate_alert("Test alert.", "de", _post=_mock_post_empty)
    assert result is None


def test_translate_alert_network_error_returns_none():
    """Network error → None (honest fallback)."""
    result = translate_alert("Test alert.", "de", _post=_mock_post_error)
    assert result is None


def test_translate_alert_empty_text_returns_none():
    """Empty input → None immediately."""
    result = translate_alert("", "de", _post=_mock_post_ok)
    assert result is None


def test_translate_alert_no_key_no_post_returns_none(monkeypatch):
    """No DASHSCOPE_API_KEY and no _post → None (LLM-off graceful)."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    result = translate_alert("Test alert.", "de")
    assert result is None


def test_en_path_no_llm_call():
    """When target_lang is 'en', the caller short-circuits — no LLM call is needed.
    translate_alert itself still works correctly (we test the caller's behaviour
    in the monitor test; here we just confirm it returns content for 'en'
    via the injected post — the monitor skips the call for 'en' lang)."""
    # The caller (run_aftercare_check) short-circuits for en; this just
    # verifies the lang module itself doesn't break for en inputs.
    result = translate_alert("Safety alert.", "en", _post=_mock_post_ok)
    assert result is not None  # injected post works regardless of lang


def test_nationality_lang_map_complete():
    """Spot-check key entries in NATIONALITY_LANG."""
    assert NATIONALITY_LANG["DE"] == "de"
    assert NATIONALITY_LANG["JP"] == "ja"
    assert NATIONALITY_LANG["CN"] == "zh"
    assert NATIONALITY_LANG["FR"] == "fr"
    assert NATIONALITY_LANG["KR"] == "ko"


# ─── caller integration: en skips LLM, None → English fallback ──────────────

def test_caller_en_no_llm_call():
    """When lang is 'en', the run_aftercare_check path sets summary_localized=summary
    and translated=True WITHOUT calling the translator. Verify by using a translator
    that raises — should NOT be called."""
    call_count = [0]

    def counting_translator(text, lang):
        call_count[0] += 1
        return "localized"

    from utils.aftercare_monitor import run_aftercare_check

    class _Store:
        def get_trip(self, idk):
            return {
                "idempotency_key": "trip-x",
                "digest": "x",
                "status": "booked",
                "user_id": "u1",
                "envelope": {
                    "legs": [{"leg_id": "leg-0", "city": "New York",
                              "checkin": "2026-10-01", "checkout": "2026-10-05"}],
                    "day_plans": [{"leg_id": "leg-0", "city": "New York", "iso2": "US",
                                   "days": []}],
                },
            }

        def get_user(self, uid):
            return {"nationality": "US", "prefs": {}}  # US → en

    from utils.emergency_feed import stub_emergency_client
    result = run_aftercare_check(
        "trip-x", "u1",
        store=_Store(),
        emergency_client=stub_emergency_client,
        translator=counting_translator,
        sender=None,
    )
    # US (en) → translator should NOT be called (en path is a trivial identity)
    assert call_count[0] == 0, "translator must NOT be called for en users"
    # US is not TW so should be clear — no alerts
    assert result["outcome"] == "ok"


def test_caller_translation_failure_english_fallback():
    """translator returns None → summary_localized = English original, translated=False."""
    from utils.aftercare_monitor import run_aftercare_check

    class _Store:
        def get_trip(self, idk):
            return {
                "idempotency_key": "trip-tw",
                "digest": "tw",
                "status": "booked",
                "user_id": "u1",
                "envelope": {
                    "legs": [{"leg_id": "leg-0", "city": "Kaohsiung",
                              "checkin": "2026-09-12", "checkout": "2026-09-15"}],
                    "day_plans": [{"leg_id": "leg-0", "city": "Kaohsiung", "iso2": "TW",
                                   "days": []}],
                },
            }

        def get_user(self, uid):
            return {"nationality": "DE", "prefs": {}}  # DE → de

    from utils.emergency_feed import stub_emergency_client
    result = run_aftercare_check(
        "trip-tw", "u1",
        store=_Store(),
        emergency_client=stub_emergency_client,
        translator=lambda text, lang: None,  # always fail
        sender=None,
    )
    assert result["outcome"] == "ok"
    assert len(result["alerts"]) >= 1
    alert = result["alerts"][0]
    # summary_localized == original English (honest fallback)
    assert alert["summary_localized"] == alert["summary"]
    assert alert["translated"] is False
    assert alert["lang"] == "de"
