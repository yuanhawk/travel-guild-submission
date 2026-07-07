"""test_telegram_notify.py — #100 AFTERCARE: Telegram suggest-only sender tests.

Tests:
- no token → {sent:False, note:'telegram disabled...'}, no HTTP attempted
- chat_id not in allowlist → {sent:False, note:'recipient not in allowlist'}
- happy path via injected _post → {sent:True}
- returned dict NEVER contains the token string
- outbound payload has NO callback_data key (only optional url button)
"""
from __future__ import annotations

import pytest

from utils.telegram_notify import send_telegram_alert


# ─── no token → disabled ─────────────────────────────────────────────────────

def test_no_token_disabled():
    http_called = []

    def fake_post(url, *, headers, json):
        http_called.append(url)
        return {"ok": True}

    result = send_telegram_alert("12345", "Alert text", token="", _post=fake_post)
    assert result["sent"] is False
    assert "telegram disabled" in result["note"].lower()
    assert len(http_called) == 0, "HTTP must NOT be called when token is empty"


# ─── chat_id not in allowlist ────────────────────────────────────────────────

def test_not_in_allowlist():
    http_called = []

    def fake_post(url, *, headers, json):
        http_called.append(True)
        return {"ok": True}

    result = send_telegram_alert(
        "99999", "Alert text",
        token="FAKE-TOKEN", allowed_users="11111,22222",
        _post=fake_post,
    )
    assert result["sent"] is False
    assert "allowlist" in result["note"].lower()
    assert len(http_called) == 0, "HTTP must NOT be called for blocked recipient"


# ─── happy path ──────────────────────────────────────────────────────────────

def test_happy_path():
    captured = {}

    def fake_post(url, *, headers, json):
        captured["url"] = url
        captured["payload"] = json
        return {"ok": True}

    result = send_telegram_alert(
        "11111", "Typhoon alert — please review on the webpage.",
        token="FAKE-TOKEN", allowed_users="11111,22222",
        _post=fake_post,
    )
    assert result["sent"] is True
    assert result.get("channel") == "telegram"
    assert "FAKE-TOKEN" in captured["url"], "token is in the URL (server-side) — expected"


# ─── token NEVER in the returned dict ────────────────────────────────────────

def test_token_never_in_response():
    """The returned dict must NOT contain the token string in any field value."""
    token = "SECRET-TOKEN-ABC123"

    def fake_post(url, *, headers, json):
        return {"ok": True}

    result = send_telegram_alert(
        "11111", "Alert",
        token=token, allowed_users="11111",
        _post=fake_post,
    )
    import json
    result_str = json.dumps(result)
    assert token not in result_str, f"Token found in response: {result_str}"


# ─── token NEVER leaks via an exception note ─────────────────────────────────

def test_token_not_in_exception_note():
    """On a network failure, httpx exceptions can embed the request URL (which holds
    the bot token). The returned note must be GENERIC and never echo the token."""
    token = "SECRET-TOKEN-XYZ789"

    def fake_post(url, *, headers, json):
        # Simulate httpx embedding the full request URL (token included) in the error.
        raise RuntimeError(f"Connection refused for url https://api.telegram.org/bot{token}/sendMessage")

    result = send_telegram_alert(
        "11111", "Alert",
        token=token, allowed_users="11111",
        _post=fake_post,
    )
    assert result["sent"] is False
    import json
    assert token not in json.dumps(result), f"Token leaked into note: {result}"
    assert result["note"] == "send failed"


# ─── no callback_data in outbound payload ────────────────────────────────────

def test_no_callback_data_in_payload():
    """Outbound Telegram payload MUST NOT contain callback_data anywhere."""
    captured = {}

    def fake_post(url, *, headers, json):
        captured["payload"] = json
        return {"ok": True}

    result = send_telegram_alert(
        "11111", "Alert text",
        token="FAKE-TOKEN", allowed_users="11111",
        webpage_url="https://example.com/trip",
        _post=fake_post,
    )
    import json
    payload_str = json.dumps(captured.get("payload", {}))
    assert "callback_data" not in payload_str, (
        f"callback_data found in outbound payload: {payload_str}"
    )


# ─── URL button has url key (not callback_data) ───────────────────────────────

def test_url_button_not_callback():
    """When webpage_url is set, the inline_keyboard button uses 'url' not 'callback_data'."""
    captured = {}

    def fake_post(url, *, headers, json):
        captured["payload"] = json
        return {"ok": True}

    send_telegram_alert(
        "11111", "Alert",
        token="FAKE-TOKEN", allowed_users="11111",
        webpage_url="https://travel.guild/trip/trip-abc123",
        _post=fake_post,
    )
    markup = captured["payload"].get("reply_markup") or {}
    keyboard = markup.get("inline_keyboard") or []
    if keyboard:
        for row in keyboard:
            for btn in row:
                assert "callback_data" not in btn, "Button must NOT have callback_data"
                assert "url" in btn, "Button must have 'url' (webpage link)"


# ─── allowlist empty → allow all (no restriction) ────────────────────────────

def test_empty_allowlist_allows_any():
    """Empty allowed_users string → no allowlist check → allow."""
    result_holder = {}

    def fake_post(url, *, headers, json):
        result_holder["called"] = True
        return {"ok": True}

    result = send_telegram_alert(
        "99999", "Alert",
        token="FAKE-TOKEN", allowed_users="",
        _post=fake_post,
    )
    assert result["sent"] is True


# ─── chat_id never echoed in response ────────────────────────────────────────

def test_chat_id_never_in_response():
    """The returned dict must NOT contain the chat_id."""
    chat_id = "PRIVATE-CHAT-ID-789"

    def fake_post(url, *, headers, json):
        return {"ok": True}

    result = send_telegram_alert(
        chat_id, "Alert",
        token="FAKE-TOKEN", allowed_users=chat_id,
        _post=fake_post,
    )
    import json
    result_str = json.dumps(result)
    assert chat_id not in result_str, f"chat_id found in response: {result_str}"
