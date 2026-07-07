"""telegram_companion.py — READ-ONLY Telegram itinerary companion for Travel Guild.

SECURITY BOUNDARY (non-negotiable):
  - READ-ONLY (TRIP DATA): This module NEVER calls save_plan / mark_booked /
    mark_cancelled / update_envelope / complete_checkout / _wallet_debit_locked /
    cancel_checkout or any transactional / mutating store method. Mutations to a
    trip/plan require authenticated web consent. The Telegram channel is
    DISPLAY-ONLY for itinerary data.
  - THE ONE DELIBERATE EXCEPTION — account linking: the '/start <token>' handler
    (see handle_start_link below) DOES call demo_users.merge_user_prefs, which
    persists chat_id into prefs.telegram_chat_id via store.upsert_demo_user. This
    is account LINKING, not trip/itinerary mutation — it is the SAME underlying
    prefs-merge/persistence path PUT /preferences uses (no independent write
    logic), and a resolved link token proves the request originated from that
    user's own logged-in web session (the token was only ever handed to them).
  - NEVER sends callback_data buttons (no inbound command path for mutations).
  - NEVER returns the bot token or chat_id in any log or response dict.
  - NEVER calls /replan, /refine, /confirm, /cancel, or any server endpoint.

var-0 SACRED:
  - This module is NEVER imported by negotiate() / _request_digest.
  - The poll loop is INERT by default: requires BOTH TELEGRAM_BOT_TOKEN AND
    TELEGRAM_POLL_ENABLED=true env vars to be set. Neither is default-on.
  - No network or file I/O at import time.
  - Telegram is display-only and MUST NEVER feed the plan/digest/deterministic path.

Inbound channel:
  Long-poll getUpdates (outbound HTTPS only; no webhook). All network calls are
  outbound HTTPS, identical in character to the send-only aftercare path.

Allowlist:
  Inbound messages from chat_ids not in TELEGRAM_ALLOWED_USERS are silently
  ignored, mirroring the send-only aftercare gating.

`_post` / `_get_updates` are injection seams so the unit tests never require real
HTTP. The poll_once() function is a single cycle — testable in isolation without
running an infinite loop.
"""
from __future__ import annotations

import html
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
_GET_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"


# ─── Itinerary formatting (pure — no network/IO) ─────────────────────────────

def format_itinerary(trip_row: dict) -> str:
    """Format a stored trip's full itinerary as a Telegram HTML string.

    Multi-leg: city + date range + per-day highlights (attractions + meals).
    Concise and Telegram HTML-safe. Pure — no network/IO, no store writes.

    Returns an honest empty message if the trip has no day_plans.
    """
    envelope = trip_row.get("envelope") or {}
    legs = envelope.get("legs") or []
    day_plans = [p for p in (envelope.get("day_plans") or []) if isinstance(p, dict)]

    if not day_plans:
        return (
            "<b>Travel Guild — Your Itinerary</b>\n\n"
            "(No itinerary data is available for this trip yet.)"
        )

    # Build leg_id → leg metadata for date lookup
    leg_meta: dict[str, dict] = {}
    for leg in legs:
        if isinstance(leg, dict):
            lid = leg.get("leg_id") or ""
            leg_meta[lid] = leg

    lines: list[str] = ["<b>Travel Guild — Your Itinerary</b>"]

    for plan_idx, plan in enumerate(day_plans):
        city = html.escape(plan.get("city") or f"Leg {plan_idx + 1}")
        leg_id = plan.get("leg_id") or ""
        meta = leg_meta.get(leg_id, plan)
        checkin = meta.get("checkin") or plan.get("checkin") or ""
        checkout = meta.get("checkout") or plan.get("checkout") or ""
        iso2 = (plan.get("iso2") or "").upper()

        # Leg header
        header = f"\n<b>{city}</b>"
        if iso2:
            header += f" ({iso2})"
        if checkin and checkout:
            header += f" | {checkin} → {checkout}"
        lines.append(header)

        days = [d for d in (plan.get("days") or []) if isinstance(d, dict)]
        if not days:
            lines.append("  (No daily plan available)")
            continue

        for day in days:
            day_num = int(day.get("day_index", 0)) + 1
            attractions = [a for a in (day.get("attractions") or []) if isinstance(a, dict)]
            meals = day.get("meals") or {}

            day_lines: list[str] = [f"\n  <b>Day {day_num}</b>"]
            if attractions:
                names = [
                    html.escape(a.get("name_en") or a.get("name") or "?")
                    for a in attractions[:3]
                ]
                day_lines.append(f"  Highlights: {', '.join(names)}")
            else:
                day_lines.append("  (No attractions scheduled)")

            # Meals: breakfast / lunch / dinner only in the summary view
            meal_parts: list[str] = []
            for slot in ("breakfast", "lunch", "dinner"):
                r = meals.get(slot)
                if isinstance(r, dict):
                    rname = r.get("name_en") or r.get("name") or ""
                    if rname:
                        meal_parts.append(f"{slot.capitalize()}: {html.escape(rname)}")
            if meal_parts:
                day_lines.append("  " + " | ".join(meal_parts))

            lines.extend(day_lines)

    lines.append(
        "\n<i>Reply 'day N' for a specific day's plan, "
        "or 'itinerary' to see this again.</i>"
    )
    return "\n".join(lines)


def format_day(trip_row: dict, day_number: int) -> str:
    """Return the detailed plan for 1-indexed day_number, across all legs in order.

    Out-of-range or missing data → an honest, helpful reply.
    Pure — no network/IO, no store writes.
    """
    envelope = trip_row.get("envelope") or {}
    day_plans = [p for p in (envelope.get("day_plans") or []) if isinstance(p, dict)]

    if not day_plans:
        return "(No itinerary data is available for this trip.)"

    # Enumerate all days across all legs in document order
    all_days: list[tuple[str, dict]] = []  # (city, day_dict)
    for plan in day_plans:
        city = plan.get("city") or ""
        for day in (plan.get("days") or []):
            if isinstance(day, dict):
                all_days.append((city, day))

    if not all_days:
        return "(No daily plans are available.)"

    total = len(all_days)
    if day_number < 1 or day_number > total:
        return (
            f"Day {day_number} is out of range — this trip has {total} day(s) "
            f"(day 1 to day {total}). "
            "Reply 'itinerary' to see the full plan."
        )

    city, day = all_days[day_number - 1]
    attractions = [a for a in (day.get("attractions") or []) if isinstance(a, dict)]
    meals = day.get("meals") or {}

    lines: list[str] = [f"<b>Day {day_number} — {html.escape(city)}</b>"]

    if attractions:
        lines.append("\n<b>Attractions:</b>")
        for a in attractions:
            name = html.escape(a.get("name_en") or a.get("name") or "?")
            cat = a.get("category") or ""
            entry = f"• {name}"
            if cat:
                entry += f" ({html.escape(cat)})"
            lines.append(entry)
    else:
        lines.append("(No attractions are scheduled for this day.)")

    meal_shown = False
    for slot in ("breakfast", "lunch", "dinner", "tea", "supper"):
        r = meals.get(slot)
        if isinstance(r, dict):
            rname = r.get("name_en") or r.get("name") or ""
            if rname:
                if not meal_shown:
                    lines.append("\n<b>Dining:</b>")
                    meal_shown = True
                lines.append(f"• {slot.capitalize()}: {html.escape(rname)}")

    lines.append("\nReply 'itinerary' to see the full trip plan.")
    return "\n".join(lines)


# ─── Query parsing (pure) ─────────────────────────────────────────────────────

# Matches: /day 3, /day3, day 3, day3, day-3, on day 3,
#          what to do on day 3, what should I do on day 3
_DAY_RE = re.compile(
    r"""(?x)
    (?:
        /day\s*(\d+)                               # /day 3 or /day3
      | (?:what\s+(?:to\s+do|should\s+i\s+do)\s+)?
        (?:on\s+)?
        \bday[\s\-]*(\d+)                          # day 3, day-3, day3, on day 3 (\b avoids "Monday 3", "holiday 5")
    )
    """,
    re.IGNORECASE,
)
_ITINERARY_RE = re.compile(r"\b(itinerary|plan|schedule|trip)\b", re.IGNORECASE)


def parse_day_query(text: str) -> int | None:
    """Return the 1-indexed day number from a user message, or None if no day query.

    Handles: 'day 3', 'day3', 'day-3', '/day 3', '/day3',
             'what to do on day 3', 'what should I do on day 3', 'on day 3'.
    Pure — no side effects.
    """
    if not text:
        return None
    m = _DAY_RE.search(text)
    if m:
        raw = m.group(1) or m.group(2)
        return int(raw) if raw else None
    return None


def is_itinerary_query(text: str) -> bool:
    """True if the message is asking for the full itinerary/plan/schedule/trip."""
    return bool(_ITINERARY_RE.search(text or ""))


# ─── chat_id → trip resolution (READ-ONLY) ───────────────────────────────────

def resolve_chat_id_to_trip(chat_id: str, *, store) -> dict | None:
    """Resolve an inbound Telegram chat_id to the sender's most recent booked trip.

    Strategy (READ-ONLY store access only):
    1. Scan all demo users; call store.get_user(user_id) to check prefs.telegram_chat_id.
    2. On match, call store.list_trips(user_id) and return the most recent booked trip
       via store.get_trip(idempotency_key).

    NEVER calls any store-write method (save_plan / mark_booked / mark_cancelled /
    update_envelope or any other mutation). Returns None if no booked trip found.
    """
    try:
        users = store.list_demo_users()
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_companion: list_demo_users failed (%s)", exc)
        return None

    for user_summary in users:
        user_id = user_summary.get("user_id") or ""
        if not user_id:
            continue
        try:
            user = store.get_user(user_id)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(user, dict):
            continue
        prefs = user.get("prefs") or {}
        stored_chat = str(prefs.get("telegram_chat_id") or "").strip()
        if stored_chat and stored_chat == str(chat_id).strip():
            return _latest_booked_trip(user_id, store=store)

    return None


def _latest_booked_trip(user_id: str, *, store) -> dict | None:
    """Return the most recent booked trip for user_id via READ-ONLY store access.

    NEVER calls any store-write method.
    """
    try:
        trips = store.list_trips(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_companion: list_trips failed for %s (%s)", user_id, exc)
        return None

    for t in trips:
        if t.get("status") == "booked":
            key = t.get("idempotency_key") or ""
            if key:
                try:
                    return store.get_trip(key)
                except Exception:  # noqa: BLE001
                    continue
    return None


# ─── Send helpers ────────────────────────────────────────────────────────────

def send_itinerary(
    trip_row: dict,
    *,
    chat_id: str,
    token: str = "",
    allowed_users: str = "",
    _post=None,
) -> dict:
    """Format and send the full itinerary for trip_row to chat_id.

    Mirrors the send_telegram_alert call path: same token / allowlist gating,
    same {sent, channel, note} return envelope. READ-ONLY — never touches the store.

    Suitable for opt-in post-booking push (e.g. calling from the /confirm handler
    after mark_booked, or from a cron that iterates booked trips). This function
    itself does NOT call mark_booked or any transactional method.
    """
    from utils.telegram_notify import send_telegram_alert
    text = format_itinerary(trip_row)
    return send_telegram_alert(
        chat_id, text,
        token=token,
        allowed_users=allowed_users,
        _post=_post,
    )


def _send_reply(
    chat_id: str,
    text: str,
    *,
    token: str,
    allowed_users: str = "",
    _post=None,
) -> dict:
    """Internal: send a reply via send_telegram_alert. Never mutates the store."""
    from utils.telegram_notify import send_telegram_alert
    return send_telegram_alert(
        chat_id, text,
        token=token,
        allowed_users=allowed_users,
        _post=_post,
    )


# ─── Account-linking: /start <token> ──────────────────────────────────────────
# The ONE deliberate exception to this module's read-only boundary — see the
# module docstring's SECURITY BOUNDARY section. Linking is account metadata
# (prefs.telegram_chat_id), not trip/itinerary data, and reuses the exact same
# prefs-merge/persistence path PUT /preferences uses (demo_users.merge_user_prefs).

_START_PREFIX = "/start "

_LINK_EXPIRED_TEXT = (
    "This connection link has expired or is invalid. Please generate a new one "
    "from the Travel Guild app (Preferences → Connect Telegram)."
)
_LINK_UNKNOWN_USER_TEXT = (
    "This connection link points to an account that could not be found. Please "
    "generate a new one from the Travel Guild app (Preferences → Connect Telegram)."
)
_LINK_SUCCESS_TEXT = (
    "✅ Connected! You'll get trip updates and safety alerts here. "
    "Ask me 'day 1' or 'itinerary' anytime to check your trip."
)


def handle_start_link(
    chat_id: str,
    link_token: str,
    *,
    store,
    token: str,
    allowed_users: str = "",
    _post=None,
) -> dict:
    """Handle an inbound '/start <token>' deep link (Telegram's standard deep-link
    convention). Resolves the SINGLE-USE token (telegram_link.resolve_link_token)
    to a user_id and persists this chat_id into that user's prefs.telegram_chat_id
    via demo_users.merge_user_prefs — the SAME persistence path PUT /preferences
    uses, so there is exactly one prefs-merge implementation.

    Never crashes on an invalid/expired token or an unresolvable user_id — both
    reply honestly instead. Never mutates trip/itinerary data.
    """
    from orchestration.demo_users import merge_user_prefs
    from utils.telegram_link import resolve_link_token

    user_id = resolve_link_token(link_token)
    if user_id is None:
        return _send_reply(
            chat_id, _LINK_EXPIRED_TEXT,
            token=token, allowed_users=allowed_users, _post=_post,
        )

    updated = merge_user_prefs(store, user_id, {"telegram_chat_id": chat_id})
    if updated is None:
        return _send_reply(
            chat_id, _LINK_UNKNOWN_USER_TEXT,
            token=token, allowed_users=allowed_users, _post=_post,
        )

    return _send_reply(
        chat_id, _LINK_SUCCESS_TEXT,
        token=token, allowed_users=allowed_users, _post=_post,
    )


# ─── Single update handler ────────────────────────────────────────────────────

_HELP_TEXT = (
    "I'm your Travel Guild itinerary companion (read-only).\n\n"
    "I can show you:\n"
    "• <b>itinerary</b> — your full trip plan\n"
    "• <b>day N</b> — what's planned for day N (e.g. 'day 2')\n\n"
    "<i>All trip changes must be made on the Travel Guild webpage.</i>"
)

_NO_TRIP_TEXT = (
    "No booked trip was found for your account. "
    "Book a trip on the Travel Guild webpage first, "
    "then use Telegram to view your itinerary."
)


def handle_update(
    update: dict,
    *,
    store,
    token: str,
    allowed_users: str = "",
    _post=None,
) -> dict | None:
    """Handle a single Telegram update dict from getUpdates.

    READ-ONLY: NEVER calls save_plan / mark_booked / mark_cancelled /
    update_envelope / complete_checkout or any other mutating store or server method.

    Returns the send-result dict (from _send_reply), or None if the update was
    silently ignored (non-allowlisted sender, non-message update, or empty text).

    Allowlist: chat_ids not in allowed_users are silently ignored (same policy as
    the send-only aftercare path).
    """
    msg = update.get("message") or update.get("edited_message") or {}
    if not isinstance(msg, dict):
        return None

    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    text = (msg.get("text") or "").strip()

    if not chat_id or not text:
        return None

    # Allowlist gate (mirrors send-side logic in send_telegram_alert)
    if allowed_users:
        allowed = [u.strip() for u in allowed_users.split(",") if u.strip()]
        if allowed and chat_id not in allowed:
            logger.debug(
                "telegram_companion: ignoring message from non-allowlisted chat_id"
            )
            return None

    # Account-linking deep link — first priority, before any trip-query dispatch.
    # Telegram's standard convention: '/start <payload>', single space, payload
    # is the token verbatim (no further parsing needed).
    if text.startswith(_START_PREFIX):
        link_token = text[len(_START_PREFIX):].strip()
        return handle_start_link(
            chat_id, link_token,
            store=store, token=token, allowed_users=allowed_users, _post=_post,
        )

    # Resolve trip — READ-ONLY (no write calls)
    trip_row = resolve_chat_id_to_trip(chat_id, store=store)

    # Dispatch by query intent
    day_num = parse_day_query(text)
    if day_num is not None:
        reply = (
            format_day(trip_row, day_num)
            if trip_row is not None
            else _NO_TRIP_TEXT
        )
        return _send_reply(
            chat_id, reply,
            token=token, allowed_users=allowed_users, _post=_post,
        )

    if is_itinerary_query(text):
        reply = (
            format_itinerary(trip_row)
            if trip_row is not None
            else _NO_TRIP_TEXT
        )
        return _send_reply(
            chat_id, reply,
            token=token, allowed_users=allowed_users, _post=_post,
        )

    # Unknown query → helpful fallback (read-only reminder included)
    return _send_reply(
        chat_id, _HELP_TEXT,
        token=token, allowed_users=allowed_users, _post=_post,
    )


# ─── Poll cycle (one-shot, testable) ─────────────────────────────────────────

def poll_once(
    *,
    token: str,
    allowed_users: str = "",
    offset: int = 0,
    store,
    _get_updates=None,
    _post=None,
) -> tuple[list[dict], int]:
    """Fetch one batch of Telegram updates and process each one.

    Returns (list_of_send_results, new_offset).

    _get_updates: callable(url, *, params) → dict  — injection seam for tests.
    _post: passed through to handle_update → _send_reply (injection seam).

    Offset advances to max(update_id) + 1 across the batch so the same update
    is never reprocessed (standard Telegram getUpdates convention). On empty
    batches or errors, the offset is returned unchanged.

    Designed to be called in a loop (run_poll_loop) but is itself a single,
    finite cycle — unit-testable without an infinite loop.
    """
    url = _GET_UPDATES_URL.format(token=token)
    params = {"timeout": 30, "offset": offset}

    try:
        if _get_updates is not None:
            resp = _get_updates(url, params=params)
        else:
            import httpx  # imported lazily: no import-time network
            r = httpx.get(url, params=params, timeout=35.0)
            resp = r.json()
    except Exception as exc:  # noqa: BLE001 — network failure must not crash the loop
        _safe = str(exc)
        if token and token in _safe:
            _safe = _safe.replace(token, "<token>")
        logger.warning("telegram_companion: getUpdates failed: %s", _safe)
        return [], offset

    if not isinstance(resp, dict) or not resp.get("ok"):
        err = (resp or {}).get("description") or "unknown"
        logger.warning("telegram_companion: getUpdates not ok: %s", err)
        return [], offset

    updates = resp.get("result") or []
    results: list[dict] = []
    new_offset = offset

    for update in updates:
        if not isinstance(update, dict):
            continue
        update_id = int(update.get("update_id") or 0)
        # Advance offset past this update (Telegram: next offset = update_id + 1)
        new_offset = max(new_offset, update_id + 1)

        result = handle_update(
            update,
            store=store,
            token=token,
            allowed_users=allowed_users,
            _post=_post,
        )
        if result is not None:
            results.append(result)

    return results, new_offset


# ─── Background poll loop ────────────────────────────────────────────────────

def run_poll_loop(
    *,
    store,
    _get_updates=None,
    _post=None,
    _sleep=None,
) -> None:
    """Background long-poll consumer. Runs until the process exits.

    INERT BY DEFAULT (var-0 safety): only starts if BOTH env vars are set:
      - TELEGRAM_BOT_TOKEN (non-empty string)
      - TELEGRAM_POLL_ENABLED in {"1", "true", "yes"} (default: not set → off)

    If either gate is absent this function returns immediately with no I/O.
    Offset is tracked in memory across the process lifetime; on restart, Telegram
    re-delivers unconfirmed updates (idempotent: same query → same read-only reply).

    _sleep: injection seam for tests to control timing.
    """
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    poll_flag = os.environ.get("TELEGRAM_POLL_ENABLED", "").strip().lower()

    if not token:
        logger.info(
            "telegram_companion: TELEGRAM_BOT_TOKEN not set — poll loop not started"
        )
        return
    if poll_flag not in ("1", "true", "yes"):
        logger.info(
            "telegram_companion: TELEGRAM_POLL_ENABLED not set/enabled — "
            "poll loop not started"
        )
        return

    allowed_users = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    sleep_fn = _sleep if _sleep is not None else time.sleep

    logger.info(
        "telegram_companion: READ-ONLY poll loop started "
        "(display-only; no mutations, no webhook)"
    )
    offset = 0
    while True:
        try:
            _, offset = poll_once(
                token=token,
                allowed_users=allowed_users,
                offset=offset,
                store=store,
                _get_updates=_get_updates,
                _post=_post,
            )
        except Exception as exc:  # noqa: BLE001 — loop must never crash
            _safe = str(exc)
            if token and token in _safe:
                _safe = _safe.replace(token, "<token>")
            logger.warning("telegram_companion: poll error: %s", _safe)
            sleep_fn(5)
