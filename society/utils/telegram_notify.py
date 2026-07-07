"""telegram_notify.py — #100 AFTERCARE: suggest-only Telegram sender.

CHANNEL-SECURITY BOUNDARY (non-negotiable):
  - SUGGEST-ONLY: sends a text message + optional URL button (opens browser to webpage).
  - NEVER uses callback_data buttons (no inbound command path back to the server).
  - NEVER returns the token or chat_id in ANY response.
  - Bot is SEND-ONLY: no getUpdates / webhook — zero inbound command capability.
  - Any rebooking deep-link is a WEBPAGE URL only (never a transactional endpoint).
  - Does NOT import or call mark_booked / mark_cancelled / complete_checkout /
    _wallet_debit_locked / cancel_checkout or any transactional symbol.

Gated: empty token → {sent:False, note:'telegram disabled (no token configured)'}.
Allowlist: chat_id must be in TELEGRAM_ALLOWED_USERS (comma-separated).

`_post` is an injection seam for tests (returns the raw Telegram API response dict).
Never raises.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_alert(
    chat_id: str,
    text: str,
    *,
    token: str = "",
    allowed_users: str = "",
    webpage_url: str = "",
    _post=None,
) -> dict:
    """SUGGEST-ONLY Telegram sender.

    Returns {"sent": bool, "channel": "telegram", "note": str}.
    NEVER returns the token or chat_id.

    Parameters
    ----------
    chat_id:       Telegram chat/user id (server-side resolved, never echoed in response).
    text:          Message body (plain text or HTML).
    token:         Bot token (server-side env; never echoed).
    allowed_users: Comma-separated allowlist of chat_ids.
    webpage_url:   Optional URL button destination (webpage only — NEVER a transaction endpoint).
    _post:         Injection seam for tests. Callable(url, headers, json) → dict.
    """
    if not token:
        return {
            "sent": False,
            "channel": "telegram",
            "note": "telegram disabled (no token configured)",
        }

    # Allowlist gate
    if allowed_users:
        allowed = [u.strip() for u in allowed_users.split(",") if u.strip()]
        if allowed and str(chat_id).strip() not in allowed:
            return {
                "sent": False,
                "channel": "telegram",
                "note": "recipient not in allowlist",
            }

    # Build the outbound payload — NEVER callback_data, only URL button
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    # Optional URL-button keyboard (opens browser; NEVER callback_data)
    if webpage_url:
        # Validate it looks like a webpage URL (not a transactional endpoint)
        # Simple check: must start with http:// or https:// or /
        if _is_webpage_url(webpage_url):
            payload["reply_markup"] = {
                "inline_keyboard": [[
                    {"text": "View on Travel Guild", "url": webpage_url}
                ]]
            }

    url = _TELEGRAM_API.format(token=token)
    try:
        if _post is not None:
            resp = _post(url, headers={"Content-Type": "application/json"}, json=payload)
        else:
            import httpx
            r = httpx.post(url, json=payload, timeout=10.0)
            resp = r.json()

        if isinstance(resp, dict) and resp.get("ok"):
            return {"sent": True, "channel": "telegram", "note": "ok"}
        err = (resp or {}).get("description") or "unknown error"
        return {"sent": False, "channel": "telegram", "note": f"api error: {err}"}
    except Exception as exc:  # noqa: BLE001 — network failure must not crash aftercare
        # Detail goes to the server log ONLY. The returned note is a GENERIC string —
        # the bot token lives in the request URL and an httpx exception can embed it,
        # so it must NEVER reach the HTTP response / FE (channel-secret boundary).
        # Redact token from URL in exception message before logging
        _safe = str(exc)
        if token and token in _safe:
            _safe = _safe.replace(token, "<token>")
        logger.warning("telegram_notify: send failed: %s", _safe)
        return {"sent": False, "channel": "telegram", "note": "send failed"}


def _is_webpage_url(url: str) -> bool:
    """True if url looks like a safe webpage URL (https://, http://, or relative /).
    Rejects data: / javascript: / other schemes."""
    if not isinstance(url, str):
        return False
    u = url.strip()
    return bool(re.match(r"^(https?://|/)", u, re.IGNORECASE))
