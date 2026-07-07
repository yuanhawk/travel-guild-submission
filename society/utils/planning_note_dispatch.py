"""planning_note_dispatch.py — #195 PROD: one-time Telegram push of the STATIC
per-leg ``planning_note`` (L3/L4 guide recommendation).

WHY A SEPARATE MODULE (not bolted onto aftercare_monitor._build_alert):
  `planning_note` is a STATIC advisory computed once at plan/booking time by
  risk_agent.py (stored on ``envelope.risk_signals.per_leg[].planning_note``). It is
  NOT a live emergency-feed field — aftercare_monitor._build_alert copies feed fields
  VERBATIM and must never carry a static note. And aftercare_monitor is STRICTLY
  READ-ONLY w.r.t. the store; this module WRITES a send-once marker, so it cannot
  live there without breaking that non-negotiable boundary.

SEND-ONCE:
  A static note must not re-fire on every periodic aftercare poll. Today the ONLY
  trigger is booking-confirm (server.py /confirm) — the natural once-per-trip boundary;
  the aftercare poll deliberately never calls this. Delivery is guarded by
  store.was_notification_sent / mark_notification_sent (KIND below). The marker is
  written ONLY after a successful send, so on a missing Telegram channel or a transient
  send failure nothing is marked and nothing is silently swallowed — but note there is
  currently NO second trigger, so such a note is not re-delivered until a future
  subscribe/preferences trigger is wired (the marker discipline keeps that send-once-safe).

CHANNEL-SECURITY BOUNDARY (mirrors telegram_notify / aftercare_monitor, non-negotiable):
  - SUGGEST-ONLY: text + URL button (webpage only). Never callback_data.
  - Reads ONLY store.get_trip + store.get_user; the sole WRITE is the send-once marker
    (store.mark_notification_sent). NEVER mark_booked/mark_cancelled/save_plan/
    complete_checkout/_wallet_debit_locked or any transactional symbol.

HONESTY: planning_note text is emitted VERBATIM from the stored risk signal — never
fabricated, never summarised. A leg with no planning_note contributes nothing.

var-0 SACRED: never imported by negotiate()/_request_digest. `now` is injected.
Never raises.
"""
from __future__ import annotations

import html
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Notification kind key for the send-once marker (store.sent_notifications.kind).
KIND = "planning_note"

_FOOTER = (
    "This is a one-time planning note recorded when you booked — not a live alert. "
    "Always recheck official government advisories before departure."
)


def extract_planning_notes(trip_row: dict) -> list[dict]:
    """Pull the STATIC per-leg planning notes from a stored trip row.

    Returns an ordered list of {leg_id, city, planning_note} for each leg that has a
    non-empty ``planning_note`` on ``envelope.risk_signals.per_leg[]``. Legs without a
    note (advisory level 1/2, or no Risk agent wired) contribute nothing. Pure; never
    raises on a malformed row (returns []).
    """
    try:
        envelope = trip_row.get("envelope") or {}
        per_leg = ((envelope.get("risk_signals") or {}).get("per_leg")) or []
    except Exception:  # noqa: BLE001 — malformed row → honest empty (docstring: never raises)
        return []
    if not isinstance(per_leg, list):  # non-iterable/scalar per_leg → nothing to surface
        return []
    out: list[dict] = []
    for i, leg in enumerate(per_leg):
        if not isinstance(leg, dict):
            continue
        note = leg.get("planning_note")
        if not note or not isinstance(note, str):
            continue
        out.append({
            "leg_id": leg.get("leg_id") or f"leg-{i}",
            "leg_index": i,
            "city": leg.get("city") or "",
            "planning_note": note,  # VERBATIM — never reworded
        })
    return out


def build_planning_note_message(notes: list[dict]) -> str:
    """Build the suggest-only Telegram body (HTML) from extracted notes. Verbatim text,
    HTML-escaped. Empty ``notes`` still returns a header-only string, but dispatch never
    sends in that case (see dispatch_planning_notes)."""
    lines = ["<b>Travel Guild — Trip Planning Guidance</b>"]
    for n in notes:
        city = html.escape(n.get("city") or "")
        note = html.escape(n.get("planning_note") or "")
        prefix = f"<b>{city}</b>: " if city else ""
        lines.append(f"\n{prefix}{note}")
    lines.append(f"\n{html.escape(_FOOTER)}")
    return "\n".join(lines)


def _webpage_url(trip_row: dict, webapp_base: str, first_leg_index: int) -> str:
    """Relative or absolute link to the trip page (webpage only — never transactional)."""
    digest = (trip_row.get("digest") or trip_row.get("idempotency_key") or "").replace("trip-", "")
    focus = f"leg-{first_leg_index}"
    rel = f"/trip/trip-{digest}?focus={focus}" if digest else f"/trip?focus={focus}"
    return (webapp_base.rstrip("/") + rel) if webapp_base else rel


def _resolve_chat_id(user: dict | None, allowed_users: str) -> str:
    """user.prefs.telegram_chat_id → first TELEGRAM_ALLOWED_USERS entry (mirrors
    aftercare_monitor._run_impl)."""
    chat_id = ""
    if user and isinstance(user.get("prefs"), dict):
        chat_id = str(user["prefs"].get("telegram_chat_id") or "").strip()
    if not chat_id and allowed_users:
        chat_id = allowed_users.split(",")[0].strip()
    return chat_id


def dispatch_planning_notes(
    idempotency_key: str,
    user_id: str = "",
    *,
    store,
    sender=None,
    webapp_base: str = "",
    now: str = "",
    token: str | None = None,
    allowed_users: str | None = None,
) -> dict:
    """Send the one-time planning-note push for a BOOKED trip, exactly once.

    Idempotent: the send-once marker guarantees a single delivery even if this is later
    called from an additional trigger (e.g. a Telegram-subscribe hook) that does not yet
    exist. Never raises.

    Outcomes (in the returned ``outcome`` field):
      unknown_trip / not_booked  — nothing to do.
      no_planning_notes          — trip has no L3/L4 note (don't mark; harmless re-eval).
      already_sent               — marker present; skip.
      telegram_disabled          — no bot token (don't mark; re-attempted only if re-triggered).
      no_channel                 — no chat_id resolved (don't mark; delivered only if re-triggered
                                   once a channel exists — no such re-trigger is wired today).
      sent                       — delivered; marker written.
      send_failed                — sender reported not-sent (don't mark; retried only if re-triggered).
      error                      — unexpected; never raised out.
    """
    try:
        return _dispatch_impl(
            idempotency_key, user_id,
            store=store, sender=sender, webapp_base=webapp_base, now=now,
            token=token, allowed_users=allowed_users,
        )
    except Exception as exc:  # noqa: BLE001 — dispatch must never crash the caller (e.g. booking)
        logger.warning("planning_note_dispatch: unexpected error (%s)", exc)
        return {
            "outcome": "error",
            "idempotency_key": idempotency_key,
            "kind": KIND,
            "notes_count": 0,
            "telegram": {"attempted": False, "sent": False, "note": "internal error"},
        }


def _dispatch_impl(
    idempotency_key: str,
    user_id: str,
    *,
    store,
    sender,
    webapp_base: str,
    now: str,
    token: str | None,
    allowed_users: str | None,
) -> dict:
    def _res(outcome: str, tg: dict | None = None, notes_count: int = 0) -> dict:
        return {
            "outcome": outcome,
            "idempotency_key": idempotency_key,
            "kind": KIND,
            "notes_count": notes_count,
            "telegram": tg or {"attempted": False, "sent": False, "note": outcome},
        }

    trip_row = store.get_trip(idempotency_key)
    if trip_row is None:
        return _res("unknown_trip")
    if trip_row.get("status") != "booked":
        return _res("not_booked")

    notes = extract_planning_notes(trip_row)
    if not notes:
        # Nothing to send — do NOT mark (re-eval is always empty, no spam risk).
        return _res("no_planning_notes")

    # Already delivered once → skip (send-once).
    if store.was_notification_sent(idempotency_key, KIND):
        return _res("already_sent", notes_count=len(notes))

    tok = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allow = allowed_users if allowed_users is not None else os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    if not tok:
        # Not configured → don't mark; a configured retry can still deliver.
        return _res("telegram_disabled")

    resolved_user_id = user_id or trip_row.get("user_id") or ""
    user = None
    if resolved_user_id:
        try:
            user = store.get_user(resolved_user_id)
        except Exception:  # noqa: BLE001 — missing user → env allowlist fallback
            user = None

    chat_id = _resolve_chat_id(user, allow)
    if not chat_id:
        # No channel yet (user hasn't linked Telegram) → don't mark; a later
        # subscribe-time dispatch delivers it then.
        return _res("no_channel", notes_count=len(notes))

    message = build_planning_note_message(notes)
    webpage_url = _webpage_url(trip_row, webapp_base, notes[0]["leg_index"])

    send = sender if sender is not None else _default_sender
    try:
        result = send(
            chat_id, message,
            token=tok, allowed_users=allow, webpage_url=webpage_url,
        )
    except Exception as exc:  # noqa: BLE001 — a Telegram failure must not crash dispatch
        logger.warning("planning_note_dispatch: telegram send failed (%s)", exc)
        return _res("send_failed",
                    {"attempted": True, "sent": False, "note": "send failed"},
                    notes_count=len(notes))

    tg = dict(result) if isinstance(result, dict) else {}
    tg["attempted"] = True
    if tg.get("sent"):
        # Delivered → write the send-once marker (the sole store WRITE in this module).
        try:
            store.mark_notification_sent(idempotency_key, KIND, now=now)
        except Exception as exc:  # noqa: BLE001 — marker failure logged, not fatal
            logger.warning("planning_note_dispatch: mark_notification_sent failed (%s)", exc)
        return _res("sent", tg, notes_count=len(notes))

    # Sender reported not-sent (disabled/allowlist/api error) → don't mark; retry later.
    return _res("send_failed", tg, notes_count=len(notes))


def _default_sender(chat_id: str, text: str, **kwargs) -> dict:
    """Real Telegram sender (suggest-only). Imported lazily so tests can inject."""
    from utils.telegram_notify import send_telegram_alert
    return send_telegram_alert(chat_id, text, **kwargs)
